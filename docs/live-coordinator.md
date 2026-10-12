# Entry coordinator and strict Live recovery acceptance

This consolidated change does not activate Live, start a broker session or worker,
change Paper settings, or register a new production service. It adds a tested
entry coordinator and a strict GitHub recovery operation. Actual broker and AWS
acceptance are separate and have not been performed in this workspace.

## Entry coordinator

`EntryCoordinator` shares an existing `AccountMonitor` lease, ledger and bounded
broker session. Its `evaluate(source)` method obtains an account-bound SFeed NSE
market-status notification, the exact admitted contract's REST bid/ask, exposure
marks and a candidate-specific margin/RMS check. It joins these with the immutable
daily cash review, complete reconciliation, current account consent, policy,
channel and source state. It makes no reservation and submits no order.

The market adapter uses the pinned Kotak SDK's typed `SFeedMarketStatus` via
`subscribe_exchange()`. Only NSE F&O status code 1 is eligible; other exchange,
closed/pre-open/auction/unknown events and absent events cannot become open-market
permission. The subscription has a three-second deadline, TLS validation, no
initial retries and no reconnects. Status collection failure returns unavailable
without discarding an otherwise valid REST session. A price or local clock never
substitutes for a market-status notification. The process boundary suppresses raw
SDK logs and does not persist credentials.

An actual account test must establish whether Kotak sends a usable current status
on a new subscription and whether the five-second total evidence budget is
achievable. If it does not, entry evaluation refuses. Do not relax this check or
invent a timestamp to make an acceptance test pass; a continuously supervised feed
may be necessary after observing actual delivery behavior.

`submit(source)` remains blocked by the static release gate. It has no argument,
API endpoint or repository-variable override. The existing protective startup
session independently refuses ENTRY commands. Synthetic tests replace the gate
and session solely to exercise the proposed handoff:

1. Collect evidence without holding an account or SQLite lock across reads.
2. Recheck the connected source, market evidence age, unchanged candidate and
   release gate inside the account lock and SQLite transaction.
3. Atomically reserve full purchase premium, bind instrument/multiplier/fees and
   immutable protective exits, mark DISPATCHING and record the daily attempt.
4. Commit, then perform one bounded command handoff while account policy/pause
   changes remain serialized. The command journal records SENDING before I/O.
5. Treat acknowledgement only as an order-ID binding, never as a fill. A timeout
   records UNKNOWN and pauses entries. No restart/resend endpoint exists.

The coordinator does not forge an updated reconciliation fingerprint after its own
reservation writes. The original snapshot covers the pre-existing state; all new
intent/plan/attempt writes share the same atomic transaction. A failure before
commit rolls back every reservation. Broker observations still have to establish
actual fills and protective capacity.

The release gate is deliberately unchanged. Production launcher/handover,
account-specific activation, static-egress/contract acceptance, actual cash/margin
observations and a supervised real-order review remain release requirements.

SDK references used for the adapter (pinned installed version 3.0.7 was inspected):
- https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/guides/websocket.md
- https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/functions/websocket/market_feed.md

## Strict AWS Live drill

After this consolidated PR is merged, **Actions → Backup operations → Run workflow
→ action: live-drill** takes a fresh private backup and restores its exact S3
version on the GitHub runner. It uses the existing infrastructure and key escrow;
no new AWS resources or locally built deployment artifacts are required.

The operation retains the existing encrypted upload/readback, object hash and
pinned key versions. Restoration runs inside a separate network namespace, with
ambient AWS credentials stripped, in a private temporary directory. The restored
portal is sanitized, pilot consent is revoked and `RECOVERY_ONLY` prevents workers
from starting. Live SQLite files must remain byte-identical to their authenticated
backup manifest; production files and workers are untouched.

Unlike a Paper-only drill, `live-drill` fails unless the restored snapshot has:

- Exactly two reviewed, approved pilot accounts, including exactly one owner.
- A Live ledger for each, with its metadata account binding intact.
- Complete order, exit, command, accounting, manual-control, cash and statement
  review schemas.
- Matching ledger UCC, decrypted account credentials and reviewed pilot UCC hash.
- Unchanged Live ledger bytes and explicit offline reconciliation requirements.

The evidence includes only aggregate counts, object/key version references and
verification time, never credential values, statements or per-account identifiers.
An empty initialized ledger can prove schema/key recovery before the first trade;
its zero order/fill counts do not claim that actual filled positions were tested.
Tests additionally cover a populated WAL ledger and two-account cash metadata.
Old backups remain restorable through additive-count compatibility, with all
original counts and file hashes still verified.

The workflow does not initialize missing production ledgers or fabricate trades.
If either account lacks a reviewed initialized Live ledger, it must fail until
that separately reviewed handover/preparation is complete. Ordinary `backup-drill`
continues to support Paper-only recovery and cannot claim strict Live acceptance.
No workflow run or S3 evidence from these new changes has yet been verified in AWS.
