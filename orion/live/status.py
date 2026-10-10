"""Read-only operator view. Never creates a ledger, authenticates or resumes."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3


# These are code/acceptance blockers, not values that a repository variable can
# override. Keep this list aligned with docs/live-release.md.
BLOCKERS = (
    'production-signal-quote-worker-and-account-routing',
    'serialized-target-trailing-and-stop-gap-policy',
    'broker-verified-cash-fees-and-dispatch-authorization',
    'verified-broker-contract-and-static-egress',
    'aws-live-ledger-restore-drill',
    'account-owner-activation-and-supervised-live-validation',
)


def release_status():
    return dict(stage='integrated-release-incomplete', live_available=False,
                order_submission_available=False, read_only_probe_available=True,
                pilot_scope='owner-plus-one-reviewed-account', blockers=list(BLOCKERS))


def account_status(root, uid, *, now=None):
    """Return counts and fixed status codes; no credentials, fills or broker IDs."""
    result = dict(state='not-started', fresh=False, checked_at=None, exposure=None,
                  uncovered=None, pending_commands=None, incident_count=None,
                  unverified_fee_fills=None, live_available=False,
                  order_submission_available=False)
    if not isinstance(uid, str) or not re.fullmatch(r'[a-f0-9]{32}', uid):
        raise ValueError('Invalid account ID')
    root = Path(root).absolute()
    path = root/'accounts'/uid/'live.db'
    # Reading a missing or recovered ledger must not initialize it. Reject all
    # symlink components, including state roots, before SQLite sees the path.
    if any(p.is_symlink() for p in (path, *path.parents)):
        return result | dict(state='review-required')
    if any((p/'RECOVERY_ONLY').exists() for p in path.parents):
        return result | dict(state='recovery-quarantine')
    if not path.exists():
        return result
    try:
        now = now or datetime.now(timezone.utc)
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            row = db.execute("SELECT account FROM metadata").fetchone()
            if not row or row[0] != uid:
                raise ValueError
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            incidents = db.execute('SELECT COUNT(*) FROM live_incidents').fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM broker_commands WHERE status!='CONFIRMED'").fetchone()[0] if 'broker_commands' in names else 0
            fees = db.execute("SELECT COUNT(*) FROM broker_fill_evidence WHERE fee_status='unverified'").fetchone()[0] if 'broker_fill_evidence' in names else 0
            result.update(incident_count=incidents, pending_commands=pending, unverified_fee_fills=fees)
            row = db.execute('SELECT * FROM live_monitor WHERE id=1').fetchone() if 'live_monitor' in names else None
            if not row:
                return result | dict(state='not-checked')
            at = datetime.fromisoformat(row['checked_at'])
            fresh = 0 <= (now-at).total_seconds() <= 30
            state = row['state'] if row['state'] in ('observed','awaiting-broker','review-required') else 'review-required'
            if incidents:
                state = 'review-required'
            elif not fresh:
                state = 'stale'
            result.update(state=state, fresh=fresh and state != 'review-required', checked_at=row['checked_at'],
                          exposure=row['exposure'], uncovered=row['uncovered'])
            return result
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        return result | dict(state='review-required', fresh=False)
