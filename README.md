# Orion India — paper pilot

A Python **paper-trading implementation** for two Telegram option-call channels, with AWS Mumbai infrastructure and GitHub Actions deployment. Includes runnable local replay, a Telegram user-account listener, a Kotak Neo market-feed connector, exact contract matching, dynamic whole-lot sizing, target exits and persistent state.

**No real orders can be submitted by this release.** It does not contain a broker order adapter. Changing `mode` to `live` raises an error. The feed integrations need authenticated account testing. The project is published to this repository and GitHub Actions validation passes; no AWS resources have been provisioned.

## Multi-user dashboard

The private multi-user pilot adds separate logins, encrypted Kotak/Telegram settings, per-user channels and risk limits, isolated paper ledgers, entry controls, instrument-level realized/open P&L and trade history. See [multi-user setup](docs/multi-user-pilot.md). Telegram authorization/channel selection and separate Kotak TOTP validation are available in the browser. Saving API details alone does not authenticate an account; starting the daily paper worker still requires the operator. The dashboard does not submit real orders.

## Try the original CLI locally

Python 3.12 is required. The replay engine and core tests use Python's standard library. Install requirements.txt for the complete test suite, dashboard, broker connectors and cloud scripts.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install --no-deps -e .
python -m unittest discover -s tests -v
python -m orion replay --config config/paper.json --master examples/instruments.synthetic.json --events examples/replay.jsonl --db /tmp/orion-demo.db
```

The example uses fictional contract economics. It should show a 3-lot simulated entry, 1 lot sold at each target, stop moving to actual entry at T1, then T1 at T2. Replaying into the same database does not duplicate events. Use a new database filename for a fresh demonstration.

## Implemented behavior

| Area | Behavior |
|---|---|
| Telegram | Two configured numeric channel IDs, user-session login, new messages and edits, no sending |
| Calls | Complete long-option BUY calls only; NIFTY/BANKNIFTY and MCX product formats in your samples |
| Contracts | Exact product, strike and CE/PE; explicit expiry wins, otherwise nearest verified unexpired expiry |
| Entry | Single BUY level needs an observed upward cross; BUY ABOVE adds configurable buffer; range enters only within range |
| Late calls | First observed tick already above a cross entry → missed; stale calls and excessive entry prices rejected |
| Sizing | Paper cash, stop-distance risk, maximum lots and open-position limits; whole lots only |
| Stop management | T1 → actual entry; T2 → T1; never loosen a stop; T3 → close remainder |
| Persistence | SQLite transaction saves deduplication keys, position/account state and audit together |
| Market data | Bid/ask simulation; targets use option bid, not provider's “target hit” messages |
| Infrastructure | Mumbai EC2, Elastic IP, encrypted disk, SSM administration, no inbound security-group rules |
| State | Versioned encrypted private S3 Terraform backend with native locking |
| Deployment | Manual GitHub OIDC workflows; review saved plan before applying; checksum-verified app installation |

Whole-lot exit allocation:

| Starting lots | Sell at T1 | Sell at T2 | Sell at T3 |
|---:|---:|---:|---:|
| 1 | 0 | 0 | 1 |
| 2 | 1 | 0 | 1 |
| 3 | 1 | 1 | 1 |
| 4 | 1 | 1 | 2 |
| 5 | 1 | 2 | 2 |
| 6 | 2 | 2 | 2 |

A one-lot position still moves its stop at T1 and T2. A stop at entry is not guaranteed net break-even after fees/slippage. All fills are simulated in full at observed bid/ask; liquidity, partial broker fills, queue position and actual statutory charges are not modeled.

## Configuration decisions

`config/paper.json` has placeholders for channel IDs and illustrative limits: ₹100,000 paper capital, ₹1,000 per-trade risk, maximum 3 lots, one open position across both channels, ₹2,000 daily realized loss entry stop, 5 entries/day, ₹25 synthetic fee per order. These are **test defaults, not your approved live risk limits**. Sizing reserves four order fees, but gap losses can exceed risk estimates. Daily loss blocks new entries; it does not liquidate existing positions based on unrealized loss.

The master supplies product-specific premium multipliers; none are inferred from screenshots. See [contract preparation](docs/contracts.md).

Index paper cutoff defaults to 15:15 IST and commodity cutoff to 22:45 IST. Both require fresh open-market quotes to simulate exits. There is no guaranteed exit if the feed is unavailable. Session calendar, special sessions and broker expiry procedures must be verified before real execution.

## Paper execution boundaries

- BTST calls are paper-only: a fresh complete call remains pending until its stated entry range is reached on the signal day, then can be held overnight. Targets, the trailed stop, expiry protection, the next-session cutoff and an explicit provider close manage the simulated exit. A BTST call that does not enter by the signal-day cutoff expires; it is never filled the next morning.
- Incomplete/split calls, ambiguous reentries, underlying-index levels, bare price updates, “Active”, and discretionary “strong momentum” or “no movement” messages.
- Edited calls: pending signals are cancelled, open position rules are not silently changed.
- Missing contracts, unknown instruments and signals outside the verified shortlist.
- Arbitrary provider text is never executed as code or passed directly to a broker.

Crude uses the same complete-call grammar but needs representative provider examples before enabling its channel product list. Text parsing is not OCR; Telegram text must be available to the account. Telegram account access is not a substitute for the provider's permission to automate use of its calls.

## Deploy and operate

Follow [setup](docs/setup.md). No Jenkins or additional always-running machine is required. Your laptop is only needed for initial setup and administration; EC2 hosts the process. This build uses daily manual Kotak TOTP authentication and does not promise unattended session renewal.

The app installer preserves configuration, Telegram session and SQLite data, installs a tested version under `/opt/orion/releases/<commit>`, changes the `current` link and leaves the service stopped. Broker/Telegram secrets belong in AWS Secrets Manager, never Git, Terraform variables, screenshots, or GitHub workflow inputs.

## Future model learning

Runtime stores source messages, timestamps, parsed decisions, quotes with available volume/OI, and simulated outcomes for operational audit. This is not a training dataset or a model-training pipeline. Later research needs authorized historical data, market snapshots before each signal, examples where no call was issued, chronological train/test splits, and evaluation with costs. A model can estimate observable patterns; it cannot recover a provider's private reasoning from screenshots alone.

Before reusing Telegram content for model training, verify the provider's rights and Telegram's applicable content/AI terms. Operational access does not automatically authorize model training. A separate research design can use appropriately licensed market data.

## What remains before live trading

Authenticated Telegram/Kotak tests, verified instrument metadata and feed timestamps, MCX API entitlements, chosen risk limits, and a real broker adapter with idempotent order reconciliation, partial-fill accounting, broker-held protective stops, stop-modification acknowledgements, restart recovery and expiry management. Paper stop movements are not broker stop orders.

See [validation](docs/validation.md) and [source references](docs/sources.md).
