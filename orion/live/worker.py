"""Combined account lifecycle for source admission and existing-exposure control.

No BUY dispatch, activation, production service registration or automatic login.
A caller supplies an explicitly authenticated bounded session factory and owns
shutdown. Every network dependency is injectable for isolated acceptance tests.
"""
import asyncio
from datetime import datetime, timezone

from .alerts import health_event
from .ledger import Refused
from .monitor import AccountMonitor
from .source import receive_signals, telegram_client


async def serve_account(store, uid, master_provider, session_factory, *, stop,
                        strategy=False, collect_trades=True, poll_seconds=2,
                        _client_factory=telegram_client):
    """Share one lease/ledger/session; source loss never abandons exposure.

    Broker calls are serialized on the ledger-owning thread. The bounded session
    timeout caps a call; source freshness is rechecked after any resulting delay.
    No second login, background resend, or automatic strategy binding occurs.
    """
    if isinstance(poll_seconds,bool) or not isinstance(poll_seconds,(int,float)) or not 0.01 <= poll_seconds <= 30:
        raise Refused('Bounded monitoring interval required')
    if stop.is_set():
        return
    with AccountMonitor(store,uid,session_factory,strategy=strategy,collect_trades=collect_trades) as account:
        account.ledger.pause()
        db = account.ledger.db
        db.execute('''CREATE TABLE IF NOT EXISTS live_worker_health(
            id INTEGER PRIMARY KEY CHECK(id=1), checked_at TEXT NOT NULL,
            source_state TEXT NOT NULL, monitor_state TEXT NOT NULL,
            cycles INTEGER NOT NULL, stopped INTEGER NOT NULL)''')
        cycles = 0
        source_state, monitor_state = 'starting', 'not-checked'
        def health(stopped=False):
            db.execute('INSERT OR REPLACE INTO live_worker_health VALUES(1,?,?,?,?,?)',
                       (datetime.now(timezone.utc).isoformat(),source_state,monitor_state,cycles,int(stopped)))
        source_stop = asyncio.Event()
        source = asyncio.create_task(receive_signals(store,uid,account.ledger,master_provider,
                                      stop=source_stop,_client_factory=_client_factory))
        source_failure_recorded = False
        health()
        try:
            while not stop.is_set():
                # Give Telegram callbacks time on the same SQLite-owning thread.
                await asyncio.sleep(0)
                if source.done():
                    failed = source.cancelled() or source.exception() is not None
                    source_state = 'disconnected' if failed else 'stopped'
                    account.ledger.pause()
                    if not source_failure_recorded:
                        health_event(account.ledger,'review-required',0)
                        source_failure_recorded = True
                else:
                    row = db.execute("SELECT 1 FROM sqlite_master WHERE name='live_source_health'").fetchone()
                    state = db.execute('SELECT state FROM live_source_health WHERE id=1').fetchone() if row else None
                    source_state = state[0] if state else 'starting'
                try:
                    value = account.cycle_from_broker() if strategy else account.cycle()
                    monitor_state = value['state']
                except Exception:
                    account.ledger.pause()
                    monitor_state = 'review-required'
                    # No reauthentication/replacement session. The existing
                    # adapter itself refuses calls after a transport failure.
                cycles += 1
                health()
                try:
                    await asyncio.wait_for(stop.wait(),timeout=poll_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            account.ledger.pause()
            source_stop.set()
            try:
                await asyncio.wait_for(source,timeout=15)
            except (Exception, asyncio.CancelledError):
                source.cancel()
                await asyncio.gather(source,return_exceptions=True)
            source_state = 'stopped'
            health(stopped=True)
