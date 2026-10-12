"""Account-leased protection worker component used by explicit monitoring startup.

It manages existing exposure only. It cannot reserve or submit a BUY, activate
Live, replace account credentials, or start/stop another worker. Tests use an
in-memory broker, never a real brokerage session.
"""
from datetime import datetime, timezone
import fcntl
import json

from .commands import Commands
from .kotak import OrderRequest
from .ledger import Ledger, Refused, amount
from .observations import Observations
from .protection import Protection, check_conflicts


class ProtectionMonitor:
    def __init__(self, ledger, session, *, collect_trades=False):
        self.collect_trades = collect_trades
        self.ledger, self.db, self.session = ledger, ledger.db, session
        self.protection = Protection(ledger)
        self.commands = Commands(ledger, session)
        self.observations = Observations(ledger, session.ucc, self.commands)
        from .manual import install
        install(self.db)
        from .accounting import Accounting
        from .cash import CashAllocation
        from .execution import install_execution_tables
        install_execution_tables(ledger)
        CashAllocation(Accounting(ledger, session.ucc))
        from .statements import install as install_statements
        install_statements(self.db)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS protection_policy(
                entry_tag TEXT PRIMARY KEY, stop_limit TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS live_monitor(
                id INTEGER PRIMARY KEY CHECK(id=1), checked_at TEXT NOT NULL,
                state TEXT NOT NULL, exposure INTEGER NOT NULL,
                uncovered INTEGER NOT NULL, action TEXT NOT NULL);
        ''')

    def bind_stop_limit(self, entry_tag, stop_limit):
        """Explicit reviewed per-entry limit, never guessed from a price gap."""
        entry = self.ledger.get(entry_tag)
        body = json.loads(entry['body'])
        terms = self.db.execute('SELECT * FROM execution_terms WHERE tag=?', (entry_tag,)).fetchone()
        if not terms:
            raise Refused('Execution terms required')
        # Validate the same precision, sizing and ordering that transport enforces.
        OrderRequest(tag=entry_tag, symbol=body['symbol'], quantity=body['quantity'],
                     lot_size=terms['lot_size'], tick=terms['tick'], price=stop_limit,
                     kind='STOP', trigger=terms['stop']).parameters()
        value = str(amount(stop_limit))
        with self.ledger.transaction():
            old = self.db.execute('SELECT stop_limit FROM protection_policy WHERE entry_tag=?', (entry_tag,)).fetchone()
            if old and amount(old[0]) != amount(value):
                raise Refused('Bound stop limit cannot change')
            self.db.execute('INSERT OR IGNORE INTO protection_policy VALUES(?,?)', (entry_tag, value))

    def health(self):
        """Counts describe the last observed book, not newly acknowledged orders."""
        row = self.db.execute('SELECT * FROM live_monitor WHERE id=1').fetchone()
        return dict(row) if row else dict(state='not-checked', exposure=None, uncovered=None)

    def _health(self, state, action):
        exposure = uncovered = 0
        for entry in self.db.execute('SELECT * FROM intents'):
            exits = self.db.execute('SELECT * FROM protective_exits WHERE entry_tag=?', (entry['tag'],)).fetchall()
            remaining = entry['filled'] - sum(r['filled'] for r in exits)
            exposure += max(0, remaining)
            # ACK/PREPARED/DISPATCHING/UNKNOWN are never counted as protection.
            protected = sum(r['quantity']-r['filled'] for r in exits if r['status'] in ('OPEN','PARTIAL') and amount(r['trigger']) > 0)
            uncovered += max(0, remaining-protected)
        from .alerts import health_event
        with self.ledger.transaction():
            self.db.execute('INSERT OR REPLACE INTO live_monitor VALUES(1,?,?,?,?,?)',
                            (datetime.now(timezone.utc).isoformat(), state, exposure, uncovered, action))
            health_event(self.ledger, state, uncovered)
        return self.health()

    def cycle(self, *, entries_allowed=False, marks=None):
        """Read/reconcile, then at most one durable command; never sends entries.

        False is the default and cancels outstanding entries even without fills.
        Partial fills always trigger remainder cancellation regardless of this
        flag. Permission withdrawal does not abandon already-confirmed exposure.
        """
        if type(entries_allowed) is not bool:
            raise Refused('Explicit entry state required')
        try:
            if self.collect_trades:
                snapshot = self.session.request('evidence')
                self.observations.ingest_evidence(snapshot)
            else:
                snapshot = self.session.request('snapshot')
                self.observations.ingest_snapshot(snapshot)
            if not entries_allowed:
                self.ledger.pause()
            check_conflicts(self.db)
            from .manual import process
            manual = process(self, marks=marks or {})
            if manual is not None:
                return manual
            # Rejected/cancelled protection is not an invitation to submit an
            # endless replacement loop. Escalate once and keep reading books.
            if self.db.execute("SELECT 1 FROM live_incidents WHERE code IN ('unprotected-exposure','monitor-requires-review')").fetchone():
                return self._health('review-required', 'none')
            for entry in self.db.execute('SELECT * FROM intents ORDER BY rowid').fetchall():
                if entry['status'] in ('OPEN','PARTIAL') and (entry['filled'] or not entries_allowed):
                    prior = self.db.execute("SELECT status FROM broker_commands WHERE kind='ENTRY' AND tag=? AND operation='cancel'", (entry['tag'],)).fetchone()
                    if not prior:
                        self.commands.cancel('ENTRY', entry['tag'])
                        return self._health('awaiting-broker', 'entry-cancel-requested')
                if not entry['filled']:
                    continue
                exits = self.db.execute('SELECT * FROM protective_exits WHERE entry_tag=?', (entry['tag'],)).fetchall()
                if any(r['status'] in ('PREPARED','DISPATCHING','UNKNOWN','CANCEL_PENDING') for r in exits):
                    return self._health('review-required', 'unresolved-protection')
                uncovered = self.protection._unreserved(entry['tag'])
                if not uncovered:
                    continue
                terms = self.db.execute('SELECT * FROM execution_terms WHERE tag=?', (entry['tag'],)).fetchone()
                policy = self.db.execute('SELECT stop_limit FROM protection_policy WHERE entry_tag=?', (entry['tag'],)).fetchone()
                if not terms or not policy or uncovered % terms['lot_size']:
                    self.protection.incident('monitor-requires-review')
                    return self._health('review-required', 'protection-policy-or-quantity')
                row = self.protection.prepare(entry['tag'], trigger_price=terms['stop'],
                                               tick_size=terms['tick'], lot_size=terms['lot_size'])
                self.protection.dispatch(row['tag'])
                self.commands.place('STOP', row['tag'], stop_limit=policy['stop_limit'])
                return self._health('awaiting-broker', 'protection-requested')
            pending = self.db.execute("SELECT 1 FROM broker_commands WHERE status!='CONFIRMED'").fetchone()
            return self._health('awaiting-broker' if pending else 'observed', 'none')
        except Exception:
            self.ledger.pause()
            # Preserve exposure counts but never imply the previous book is fresh.
            self._health('review-required', 'broker-cycle-failed')
            raise Refused('Protection monitor requires review') from None


class AccountMonitor:
    """Lease the same account slot as Paper before opening an existing live DB.

    The session factory is invoked only AFTER the lease is acquired. A blocked
    Paper worker slot therefore cannot trigger a competing broker login.
    Neither Paper nor Live startup may bypass this lease.
    """
    def __init__(self, store, uid, session_factory, *, strategy=False, collect_trades=False):
        if type(strategy) is not bool or type(collect_trades) is not bool:
            raise Refused('Explicit worker management options required')
        self.strategy, self.collect_trades = strategy, collect_trades
        self.store, self.uid, self.session_factory = store, uid, session_factory
        self.session = None
        self.lease = self.ledger = None
        self.entry_authority = None

    def __enter__(self):
        self.store.user(self.uid)
        account = self.store.account_dir(self.uid)
        self.lease = open(account/'worker.lock', 'a')
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.ledger = self._open_ledger(account/'live.db')
            self.session = self.session_factory()
            manager = ProtectionMonitor
            if self.strategy:
                from .strategy import ExitStrategy
                manager = ExitStrategy
            self.monitor = manager(self.ledger, self.session, collect_trades=self.collect_trades)
            return self
        except BaseException:
            self.__exit__()
            raise Refused('Account worker slot unavailable or ledger invalid') from None

    def _open_ledger(self, path):
        if not path.is_file():
            raise Refused('Existing live ledger required')
        return Ledger(path, self.uid)

    def bind_strategy(self, entry_tag, *, targets, stop_limit, target_timeout):
        """Transfer final confirmed exposure to exits without releasing the lease.

        The caller must supply the previously reviewed signal/exit policy. This
        method cannot submit an entry or stop/restart a Paper worker.
        """
        if self.ledger is None or self.session is None:
            raise Refused('Account worker lease required')
        from .strategy import ExitStrategy
        with self.store.lock(self.uid):
            manager = ExitStrategy(self.ledger, self.session, collect_trades=self.collect_trades)
            manager.bind(entry_tag, targets=targets, stop_limit=stop_limit, target_timeout=target_timeout)
            self.monitor, self.strategy = manager, True

    def cycle_from_broker(self):
        """Use bound broker bids for existing exits; no entry authorization."""
        if self.ledger is None or self.session is None:
            raise Refused('Leased strategy worker required')
        from .marks import collect_marks
        with self.store.lock(self.uid):
            try:
                marks = collect_marks(self.ledger, self.session)
            except Exception:
                self.ledger.pause()
                result = self.monitor.cycle(marks={})
                return self.monitor._health('review-required', 'broker-quote-failed:' + result['action'])
            if self.strategy:
                return self.monitor.cycle(marks=marks)
            return self.monitor.cycle(marks=marks, entries_allowed=self._entries_allowed())

    def _entries_allowed(self):
        return bool(self.entry_authority is not None and self.entry_authority.allowed())

    @property
    def needs_quotes(self):
        return self.strategy or bool(self.ledger and self.ledger.db.execute(
            "SELECT 1 FROM manual_requests WHERE action='EXIT' AND state='PENDING'").fetchone())

    def request_manual(self, **instruction):
        """Internal worker API; authentication/confirmation belongs to its caller."""
        if self.ledger is None or self.session is None or self.lease is None or self.lease.closed:
            raise Refused('Active account worker lease required')
        from .manual import request
        with self.store.lock(self.uid):
            return request(self.monitor, **instruction)

    def cycle(self, *, marks=None):
        if self.ledger is None:
            raise Refused('Account worker lease required')
        # Missing or expired one-shot permission closes outstanding entry orders;
        # confirmed exposure retains protective management.
        with self.store.lock(self.uid):
            if self.strategy:
                return self.monitor.cycle(marks={} if marks is None else marks)
            result = self.monitor.cycle(entries_allowed=self._entries_allowed())
            if result['state'] != 'review-required':
                from .entry_plans import handoff
                try:
                    manager = handoff(self.monitor)
                    if manager is not None:
                        self.monitor, self.strategy = manager, True
                except Exception:
                    self.ledger.pause()
                    return self.monitor._health('review-required', 'exit-handoff-requires-review')
            return result

    def __exit__(self, *unused):
        # A failed SDK/process cleanup must not leak the account lease or database.
        try:
            if self.session is not None:
                self.session.close()
        finally:
            self.session = None
            try:
                if self.ledger is not None:
                    self.ledger.close()
            finally:
                self.ledger = None
                if self.lease is not None:
                    self.lease.close()
                    self.lease = None
