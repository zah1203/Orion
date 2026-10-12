# Reconcile Paper cash without resetting trades

Open Home → **Paper balance explained** → **Check balance and history**. The owner
can also check a selected account. This is an on-demand snapshot; refresh it to
include later trades. It does not stop a worker, log into Kotak, change cash,
rewrite history, clear an incident or enable Live.

The existing balance-adjustment operation sets **current available cash** to a
target. It is not an instruction to change initial capital or add a deposit.
Historical P&L stays unchanged. To add simulated funds, the target must include
the current balance plus the desired addition. Do not adjust cash just to make
the dashboard agree with an expected number.

The report checks:

```
net contributed capital = configured starting cash + sum(after − before)
expected cash = net contributed capital + realized P&L − remaining position cost
unexplained difference = recorded cash − expected cash
```

Remaining cost uses each filled position's saved entry price, premium multiplier
and remaining lots, including partial exits. It is not the marked market value.
Reported equity separately includes the market value of remaining positions.
With no open positions, equity equals cash; historical profits may already have
been included in a later target-balance adjustment.

For illustration only: start with ₹200,000, earn ₹38,292.50, then **set cash** to
₹300,000. The recorded addition is ₹61,707.50, not ₹100,000. If another ₹19,407.50
is earned, cash is ₹319,407.50 and historical profit is ₹57,700. Net contributed
capital is ₹261,707.50. These synthetic numbers demonstrate the arithmetic; they
do not establish what happened in a production account.

The authenticated `GET /api/paper-capital` reads only the signed-in account.
`GET /api/admin/users/{uid}/paper-capital` requires the approved owner. Both read
state and the full relevant audit in one SQLite read transaction, including
committed WAL data. They never instantiate the trading engine. A missing ledger
returns `not-started`; malformed data returns a generic review error.

`consistent` means retained records agree with the configured starting balance:
position and daily P&L totals agree; trade quantities, cumulative P&L, recorded
fee/delta checkpoints and final holdings agree; replayed cash agrees; and each
balance adjustment's before/after values match. It is **not** independent proof
that historical records were never altered, the starting balance was correct,
broker funds are available or Live execution is ready. Missing legacy audit
checkpoints, changed quantities/fees, invalid adjustments and unexplained drift
produce `review-required`. No inferred historical fees or automatic repairs.

Review the full adjustment list and issues before concluding that any reported
gap is explained. Preserve an encrypted verified backup before a separately
reviewed repair. Never reset the account, erase history, or stop workers to run
this check. Existing Live activation remains rejected regardless of the result.
