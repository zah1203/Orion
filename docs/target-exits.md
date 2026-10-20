# Lot-aware target exits

New entries use this whole-lot policy:

| Starting lots | T1 | T2 | T3 |
| ---: | ---: | ---: | ---: |
| 1 | 1 | 0 | 0 |
| 2 | 1 | 1 | 0 |
| 3 | 1 | 1 | 1 |
| 4 | 1 | 1 | 2 |
| 5 | 1 | 1 | 3 |
| 6 | 1 | 1 | 4 |

At T1, close one lot. If exposure remains, move its stop to actual entry.
At T2, close one lot. If exposure remains, move its stop to T1.
At T3, close the remainder. Once flat, no later targets or trailing moves apply.
This changes no configured lot cap, sizing, cash, risk, target prices, fee model,
market-data freshness rule, order timeout, or production/MCX activation gate.

## Paper

`Engine._quote` freezes `allocations(lots)` when the simulated BUY fills. Existing
OPEN positions keep their saved `allocations`, stage, stop and audit history.
Pending unfilled paper signals use the new policy if they enter after the upgrade.
The target loop ends immediately on closure, including a quote above several
targets. Paper still fills at observed bid; a gap can therefore fill several
allocated exits at that same bid. Only exits with nonzero quantity incur a fee.

## Live implementation, still gated

`EntryPlans._bind_locked` saves allocation version 2 before entry dispatch in the
same transaction as the existing exit policy. Once an entry is final, both
`entry_plans.handoff` and `PortfolioMonitor._bind_ready` pass that saved version to
`ExitStrategy.bind`. Allocations use final confirmed filled whole lots, including
an entry partially filled then cancelled. A still-working partial entry cannot
bind a fixed exit allocation; existing protective monitoring remains in charge.

For version 2, T1 trailing uses the final reconciled weighted-average entry fill,
rounded **down** to the contract tick, and never below the original stop. An
average of 99.975 with a 0.05 tick gives 99.95. Missing or invalid average evidence
is refused, not replaced with an assumed entry price. Execution and additional
entry admission share the same expected-stop calculation.

Reaching a price alone does not confirm a sale. The existing live state machine
still waits for broker-observed stop cancellations, target fills and replacement
stop acknowledgements. A partially filled target does not advance its stage.
Cancelled targets retain their recovery journal, re-protect confirmed remaining
exposure and require a fresh retreat before rearming. Odd-lot exposure and unknown
commands keep their review gates. A single gap quote never bypasses these states.

The live path retains its bounded limit-order behavior: no market order or fill
guarantee is introduced. A stop near entry can still lose money after rounding,
fees, slippage or a stop-limit gap.

## Upgrade compatibility and rollout

- Paper: no existing position or historical audit is migrated or recalculated.
- Live: added `allocation_version` columns default existing rows to version 1.
  Old entry plans waiting for final fills also remain version 1, even if the
  strategy has not yet been bound. Both handoff paths recognize pre-column schemas.
- Version 1 retains its original allocations (one lot at T3; two lots at T1/T3;
  thirds for larger positions) and entry-limit-based trailing stop.
- A repeat bind preserves the saved version and rejects changed exits. Neither
  schema migration nor restart rewrites quantities, stage, phase, recovery fills,
  cash, fee records or trade history. Unknown versions fail closed.
- Do not manually rewrite existing plans to version 2. New behavior starts with
  newly filled paper entries or newly bound pre-dispatch live entry plans.
- Release publication, merge, deployment and live activation are separate actions.
  Before any approved rollout, use the normal account isolation, backup, worker
  pause/handover and release verification procedures. Confirm a new synthetic
  one-lot entry closes at T1 and a saved legacy fixture retains its prior plan.
- Do not downgrade to code unaware of this schema/version: its inserts, bind and
  stop rules are incompatible. Preserve the database and use a compatible release.
  Any rollback needs a separately reviewed schema/data plan and verified flat
  exposure; flat exposure alone does not make an old binary schema-compatible.

## Regression coverage

The synthetic tests cover one through six lots, exact quantities and realized
P&L/fees, stop reversals, gap quotes, no activity after closure, final partial
entries, partial target fills, whole-lot timeout recovery, restart/deduplication,
old schemas and unbound legacy plans, weighted-average rounding and admission
consistency. No real broker account, credentials or order is used.
