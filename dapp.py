from os import environ
import json
import logging

import pandas as pd
import requests

logging.basicConfig(level="INFO")
logger = logging.getLogger(__name__)

rollup_server = environ["ROLLUP_HTTP_SERVER_URL"]
logger.info(f"HTTP rollup_server url is {rollup_server}")

# ---------------------------------------------------------------------------
# Mock seed data — three lending pools with pre-seeded positions
# ---------------------------------------------------------------------------

pools_df = pd.DataFrame(
    {
        "asset": ["ETH", "USDC", "DAI"],
        "base_rate": [0.02, 0.03, 0.025],
        "slope": [0.15, 0.10, 0.12],
    }
)

positions_df = pd.DataFrame(
    {
        "user": [
            "0xAlice",
            "0xAlice",
            "0xBob",
            "0xBob",
            "0xCharlie",
            "0xCharlie",
        ],
        "asset": ["ETH", "USDC", "ETH", "DAI", "USDC", "ETH"],
        "type": ["supply", "supply", "supply", "supply", "supply", "borrow"],
        "amount": [10.0, 5000.0, 5.0, 3000.0, 8000.0, 2.0],
    }
)

transactions_df = pd.DataFrame(
    columns=["user", "asset", "action", "amount", "timestamp"]
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def hex_encode(text: str) -> str:
    return "0x" + text.encode("utf-8").hex()


def emit_report(payload: dict):
    requests.post(
        rollup_server + "/report",
        json={"payload": hex_encode(json.dumps(payload, default=str))},
    )


def emit_notice(payload: dict):
    requests.post(
        rollup_server + "/notice",
        json={"payload": hex_encode(json.dumps(payload, default=str))},
    )


def compute_pool_stats() -> pd.DataFrame:
    """Merge pool config with aggregated positions to compute utilization & APYs."""
    supply = (
        positions_df[positions_df["type"] == "supply"]
        .groupby("asset")["amount"]
        .sum()
        .rename("total_supplied")
    )
    borrows = (
        positions_df[positions_df["type"] == "borrow"]
        .groupby("asset")["amount"]
        .sum()
        .rename("total_borrowed")
    )

    stats = pools_df.merge(supply, on="asset", how="left").merge(
        borrows, on="asset", how="left"
    )
    stats["total_supplied"] = stats["total_supplied"].fillna(0)
    stats["total_borrowed"] = stats["total_borrowed"].fillna(0)

    stats["utilization"] = stats.apply(
        lambda r: r["total_borrowed"] / r["total_supplied"]
        if r["total_supplied"] > 0
        else 0,
        axis=1,
    )
    stats["borrow_apy"] = stats["base_rate"] + stats["slope"] * stats["utilization"]
    stats["supply_apy"] = stats["borrow_apy"] * stats["utilization"]
    return stats


def user_portfolio(user: str) -> dict:
    """Build a per-asset summary for a user via pivot_table."""
    user_pos = positions_df[positions_df["user"] == user]
    if user_pos.empty:
        return {"user": user, "positions": []}

    pivot = user_pos.pivot_table(
        index="asset", columns="type", values="amount", aggfunc="sum", fill_value=0
    ).reset_index()
    return {"user": user, "positions": pivot.to_dict(orient="records")}


# ---------------------------------------------------------------------------
# Advance handler — state-changing operations
# ---------------------------------------------------------------------------


def handle_advance(data):
    global positions_df, transactions_df

    try:
        payload = json.loads(bytes.fromhex(data["payload"][2:]))
    except Exception:
        emit_report({"error": "invalid payload — expected JSON hex"})
        return "reject"

    action = payload.get("action")
    asset = payload.get("asset")
    amount = float(payload.get("amount", 0))
    user = data.get("metadata", {}).get("msg_sender", "0xUnknown")

    logger.info(f"advance | {action} {amount} {asset} from {user}")

    if action not in ("deposit", "borrow", "repay", "withdraw"):
        emit_report({"error": f"unknown action: {action}"})
        return "reject"

    if asset not in pools_df["asset"].values:
        emit_report({"error": f"unsupported asset: {asset}"})
        return "reject"

    if amount <= 0:
        emit_report({"error": "amount must be positive"})
        return "reject"

    # -- deposit: add a supply position -----------------------------------
    if action == "deposit":
        new_row = pd.DataFrame(
            [{"user": user, "asset": asset, "type": "supply", "amount": amount}]
        )
        positions_df = pd.concat([positions_df, new_row], ignore_index=True)

    # -- borrow: check collateral then add borrow position ----------------
    elif action == "borrow":
        user_supplies = (
            positions_df[
                (positions_df["user"] == user) & (positions_df["type"] == "supply")
            ]["amount"].sum()
        )
        user_borrows = (
            positions_df[
                (positions_df["user"] == user) & (positions_df["type"] == "borrow")
            ]["amount"].sum()
        )
        if (user_borrows + amount) > user_supplies * 0.75:
            emit_report(
                {"error": "insufficient collateral (75% max LTV)"}
            )
            return "reject"

        new_row = pd.DataFrame(
            [{"user": user, "asset": asset, "type": "borrow", "amount": amount}]
        )
        positions_df = pd.concat([positions_df, new_row], ignore_index=True)

    # -- repay: reduce or remove matching borrow --------------------------
    elif action == "repay":
        mask = (
            (positions_df["user"] == user)
            & (positions_df["asset"] == asset)
            & (positions_df["type"] == "borrow")
        )
        if not mask.any():
            emit_report({"error": "no borrow position to repay"})
            return "reject"
        idx = positions_df[mask].index[0]
        positions_df.at[idx, "amount"] -= amount
        if positions_df.at[idx, "amount"] <= 0:
            positions_df = positions_df.drop(idx).reset_index(drop=True)

    # -- withdraw: reduce or remove matching supply -----------------------
    elif action == "withdraw":
        mask = (
            (positions_df["user"] == user)
            & (positions_df["asset"] == asset)
            & (positions_df["type"] == "supply")
        )
        if not mask.any():
            emit_report({"error": "no supply position to withdraw"})
            return "reject"
        idx = positions_df[mask].index[0]
        positions_df.at[idx, "amount"] -= amount
        if positions_df.at[idx, "amount"] <= 0:
            positions_df = positions_df.drop(idx).reset_index(drop=True)

    # Record transaction (pd.concat)
    tx = pd.DataFrame(
        [
            {
                "user": user,
                "asset": asset,
                "action": action,
                "amount": amount,
                "timestamp": pd.Timestamp.now().isoformat(),
            }
        ]
    )
    transactions_df = pd.concat([transactions_df, tx], ignore_index=True)

    # Emit notice with the user's updated portfolio (groupby + pivot_table)
    emit_notice(
        {
            "action": action,
            "asset": asset,
            "amount": amount,
            "portfolio": user_portfolio(user),
        }
    )
    return "accept"


# ---------------------------------------------------------------------------
# Inspect handler — read-only analytics queries
# ---------------------------------------------------------------------------


def handle_inspect(data):
    payload_hex = data["payload"][2:]
    route = bytes.fromhex(payload_hex).decode("utf-8")
    logger.info(f"inspect | route={route}")

    # /inspect/pools — pool stats via merge + groupby + computed columns
    if route == "pools":
        stats = compute_pool_stats()
        emit_report(
            {
                "route": "pools",
                "pools": stats.round(4).to_dict(orient="records"),
            }
        )

    # /inspect/positions/<user> — user positions via filter + pivot_table
    elif route.startswith("positions/"):
        user = route.split("/", 1)[1].lower()
        emit_report({"route": "positions", **user_portfolio(user)})

    # /inspect/top_suppliers — leaderboard via groupby + nlargest
    elif route == "top_suppliers":
        supplies = (
            positions_df[positions_df["type"] == "supply"]
            .groupby("user")["amount"]
            .sum()
            .nlargest(10)
            .reset_index()
            .rename(columns={"amount": "total_supplied"})
        )
        emit_report(
            {
                "route": "top_suppliers",
                "leaderboard": supplies.to_dict(orient="records"),
            }
        )

    # /inspect/history — transaction log + describe() summary stats
    elif route == "history":
        report = {"route": "history"}
        if transactions_df.empty:
            report["transactions"] = []
            report["summary"] = "no transactions yet"
        else:
            report["transactions"] = transactions_df.to_dict(orient="records")
            report["summary"] = (
                transactions_df.describe(include="all").to_dict()
            )
        emit_report(report)

    else:
        emit_report(
            {
                "error": f"unknown route: {route}",
                "available": ["pools", "positions/<user>", "top_suppliers", "history"],
            }
        )

    return "accept"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

handlers = {
    "advance_state": handle_advance,
    "inspect_state": handle_inspect,
}

finish = {"status": "accept"}

while True:
    logger.info("Sending finish")
    response = requests.post(rollup_server + "/finish", json=finish)
    logger.info(f"Received finish status {response.status_code}")
    if response.status_code == 202:
        logger.info("No pending rollup request, trying again")
    else:
        rollup_request = response.json()
        handler = handlers[rollup_request["request_type"]]
        finish["status"] = handler(rollup_request["data"])
