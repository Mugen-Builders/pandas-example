# Pandas Example - Simple Borrow/Lend Feature

A Cartesi Rollups mock application that showcases **pandas** library usage by simulating a simplified AAVE-like lending platform. All state is stored in pandas DataFrames with analytics computed via `groupby`, `merge`, `pivot_table`, and more.

## How the App Works

The application models a simplified DeFi lending protocol inspired by AAVE. Three in-memory pandas DataFrames form the entire state:

- **`pools_df`** -- Configuration for each lending pool (ETH, USDC, DAI), storing `base_rate` and `slope` parameters for the interest rate model.
- **`positions_df`** -- Every user deposit (supply) and loan (borrow) as individual rows with `user`, `asset`, `type`, and `amount` columns. Pre-seeded with sample positions from mock users (Alice, Bob, Charlie).
- **`transactions_df`** -- An append-only ledger recording every action with timestamps.

### Advance inputs (state changes)

Users submit JSON payloads via Cartesi Rollups inputs. The advance handler supports four actions:

| Action | Logic | Pandas operations |
|---|---|---|
| `deposit` | Adds a supply position for the sender | `pd.DataFrame()`, `pd.concat` |
| `borrow` | Checks 75% max LTV collateral ratio, then adds a borrow position | Boolean indexing, `sum()`, `pd.concat` |
| `repay` | Finds the matching borrow row and reduces its amount in-place | Boolean masking, `.at[]` update, `drop` |
| `withdraw` | Finds the matching supply row and reduces its amount in-place | Boolean masking, `.at[]` update, `drop` |

Every action appends a record to `transactions_df` via `pd.concat` and emits a notice containing the user's updated portfolio (built with `pivot_table`).

### Inspect routes (read-only queries)

Inspect requests hit the dapp with a plain-text route and return a JSON report:

| Route | What it returns | Pandas operations |
|---|---|---|
| `pools` | Per-asset stats: total supplied, total borrowed, utilization rate, borrow APY, supply APY | `groupby().sum()`, `merge` (left join), `fillna`, `apply`, computed columns |
| `positions/<user>` | The user's net supply/borrow per asset | Boolean filtering, `pivot_table` with `aggfunc="sum"` |
| `top_suppliers` | Leaderboard of top depositors | `groupby().sum()`, `nlargest` |
| `history` | Full transaction log with summary statistics | `to_dict`, `describe(include="all")` |

### Interest rate model

The pool stats route computes a simplified AAVE-style linear interest rate model entirely with pandas vectorized operations:

```
utilization  = total_borrowed / total_supplied
borrow_apy   = base_rate + slope * utilization
supply_apy   = borrow_apy * utilization
```

These are calculated as computed columns on a merged DataFrame (`pools_df` joined with aggregated `positions_df`).

## Pandas Features Demonstrated

| Feature | Where |
|---|---|
| `pd.DataFrame` creation and seed data | Startup |
| `pd.concat` | deposit, borrow, transaction log |
| Boolean indexing (`df[df.col == val]`) | All handlers |
| `groupby` + `sum` / `agg` | Pool stats, top suppliers |
| `merge` (left join) | Pool stats computation |
| `pivot_table` | User portfolio view |
| `nlargest` | Top suppliers leaderboard |
| `fillna`, `apply` | Utilization rate calculation |
| `describe` | Transaction history summary |
| `to_dict` / `to_json` | All report outputs |
| `.at[]` in-place update | Repay, withdraw |

## Building

```bash
cartesi build
```

The build uses pre-compiled numpy/pandas wheels for RISC-V from the `wheels/` directory.

## Running

```bash
cartesi run
```

Once running, the output shows the application address and available endpoints (port may vary):

```
✔ pandas-example starting at http://127.0.0.1:6751
✔ pandas-example contract deployed at 0x<APP_ADDRESS>
```

Set these for convenience:

```bash
APP=<APP_ADDRESS from output>
RPC=http://localhost:6751/anvil
INSPECT=http://localhost:6751/inspect/pandas-example
SENDER=0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
```

### Sending advance inputs

```bash
# Deposit 20 ETH
cartesi send --rpc-url $RPC --application $APP --encoding string --from $SENDER \
  '{"action":"deposit","asset":"ETH","amount":20}'

# Deposit 5000 USDC
cartesi send --rpc-url $RPC --application $APP --encoding string --from $SENDER \
  '{"action":"deposit","asset":"USDC","amount":5000}'

# Borrow 3 ETH
cartesi send --rpc-url $RPC --application $APP --encoding string --from $SENDER \
  '{"action":"borrow","asset":"ETH","amount":3}'

# Repay 1 ETH
cartesi send --rpc-url $RPC --application $APP --encoding string --from $SENDER \
  '{"action":"repay","asset":"ETH","amount":1}'

# Withdraw 5 ETH
cartesi send --rpc-url $RPC --application $APP --encoding string --from $SENDER \
  '{"action":"withdraw","asset":"ETH","amount":5}'
```

### Querying inspect routes

Reports are returned as hex-encoded JSON in the `reports[].payload` field.

```bash
# Pool stats
curl -s -X POST $INSPECT -H "Content-Type: text/plain" -d 'pools'

# User positions
curl -s -X POST $INSPECT -H "Content-Type: text/plain" -d 'positions/0xAlice'

# Top suppliers leaderboard
curl -s -X POST $INSPECT -H "Content-Type: text/plain" -d 'top_suppliers'

# Transaction history
curl -s -X POST $INSPECT -H "Content-Type: text/plain" -d 'history'
```
