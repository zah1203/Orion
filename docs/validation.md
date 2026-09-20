# Validation record — 2026-09-20

## Completed

- 25 deterministic unit tests passed under Python 3.12.14.
- Replay of the provided NIFTY-style entry/three-target sequence produced one 3-lot simulated entry, then one lot per target; stops moved to actual entry and then T1. No duplicate fills on replay into the same database.
- One-lot trailing without fractional exits, gap-through-target exits, gap-through-stop pricing, SQLite restart persistence, repeated messages/reposts, edited pending calls, stale messages/quotes, pre-signal quotes, closed-market entry blocking, insufficient risk budget, range entries and late crossings tested.
- Exact strike/type matching, nearest verified expiry, explicit expiry without fallback, duplicate instruments, stale/synthetic masters, commodity expiry parsing and BTST review-only behavior tested.
- Python wheel built and installed; pinned runtime dependencies installed and `pip check` passed.
- Python syntax and targeted Ruff correctness checks passed.
- Four GitHub workflow YAML files parsed; Terraform HCL parsed; cloud-init shell syntax passed.
- Terraform 1.10.5 `fmt -check` passed.
- AWS provider 5.100.0 retrieved with HashiCorp signature verification; official release ZIP SHA256 checked; dependency lockfile verified for linux_amd64.

## Environment limitation

Full `terraform validate` could not complete here: the provider tried to open a local Unix socket and received `operation not permitted`. A cache mismatch discovered first was resolved; the official provider binary matches the verified release. No checksum verification was disabled. CI runs full Terraform validation on a standard GitHub-hosted runner and must pass before deployment.

## Not performed

No AWS plan/apply, SSM installation, GitHub workflow run, actual Telegram login/channel access, Kotak login, live feed, broker order, actual CSV contract normalization, margin check or real fill reconciliation was performed. No account credentials were provided. The live-input code follows the inspected SDK, but it remains unverified against the user's account.

Paper results assume complete fills at observed bid/ask; the example's ₹785 simulated gain uses fictitious contract economics and a flat illustrative fee. It is a software demonstration, not a strategy performance result.

The application has no real-order execution path and no overnight-trading implementation. MCX BTST calls remain recorded for review. Crude parsing needs representative provider examples. Account setup and these limitations are described in README.md and docs/setup.md.
