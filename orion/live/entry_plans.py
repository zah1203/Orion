"""Immutable admitted-signal exit instructions, saved before entry dispatch.

Binding is not BUY authorization: cash, fees, provenance, risk and atomic dispatch
still have separate gates. Existing exposure retains its saved exits on revocation.
"""
from datetime import datetime, timezone
import json

from .accounting import timestamp
from .ledger import Refused, amount
from .routing import SignalRouter, reviewed_policy


class EntryPlans:
    def __init__(self, store, uid, monitor):
        self.store, self.uid, self.monitor = store, uid, monitor
        self.ledger, self.db = monitor.ledger, monitor.db
        if self.ledger.account != uid:
            raise Refused('Exit plan account mismatch')
        self.router = SignalRouter(store, uid, self.ledger, monitor.session.ucc)
        self.db.execute('''CREATE TABLE IF NOT EXISTS entry_exit_plans(
            entry_tag TEXT PRIMARY KEY, source TEXT UNIQUE NOT NULL,
            source_digest TEXT NOT NULL, policy_version INTEGER NOT NULL,
            targets TEXT NOT NULL, stop_limit TEXT NOT NULL,
            target_timeout INTEGER NOT NULL)''')

    def bind(self, entry_tag):
        """Bind exact admitted source and current reviewed exit limits, no defaults.

        Only a PREPARED, undispatched matching intent can be bound. The account
        lock spans the policy read and the atomic plan/protection-policy write.
        """
        with self.store.lock(self.uid), self.ledger.transaction():
            entry = self.ledger.get(entry_tag)
            if entry['status'] != 'PREPARED' or entry['filled'] or entry['broker_id']:
                raise Refused('Undispatched entry required')
            policy = reviewed_policy(self.store, self.uid, self.monitor.session.ucc)
            source = self.db.execute('SELECT * FROM live_signals WHERE source=?', (entry['event'],)).fetchone()
            if not source or source['state'] != 'PENDING' or source['policy_version'] != policy['version']:
                raise Refused('Current admitted signal required')
            _, digest = self.router._channel(policy, source['channel'])
            age = (datetime.now(timezone.utc)-timestamp(datetime.fromisoformat(source['signal_time']))).total_seconds()
            if digest != source['channel_digest'] or not 0 <= age <= 30:
                raise Refused('Fresh unchanged signal required')
            body = json.loads(entry['body'])
            normalized = json.loads(source['body'])
            signal, contract = normalized['signal'], normalized['contract']
            terms = self.db.execute('SELECT * FROM execution_terms WHERE tag=?', (entry_tag,)).fetchone()
            instrument = self.db.execute('SELECT * FROM reconciliation_instruments WHERE entry_tag=?', (entry_tag,)).fetchone()
            if (timestamp(datetime.fromisoformat(contract['expiry_at']))-datetime.now(timezone.utc)).total_seconds() <= 1800:
                raise Refused('Unexpired admitted contract required')
            lot, tick = contract['order_quantity_per_lot'], amount(contract['tick_size'])
            price, stop = amount(body['limit_price']), amount(signal['stop'])
            targets = [amount(v) for v in signal['targets']]
            if (not terms or not instrument or instrument['product'] != 'NRML' or
                    instrument['token'] != contract['token'] or body['symbol'] != contract['symbol'] or
                    body['segment'] != contract['segment'] or body['segment'] != 'nse_fo' or
                    body['side'] != 'BUY' or body['mode'] != 'live' or
                    timestamp(datetime.fromisoformat(body['signal_time'])) != timestamp(datetime.fromisoformat(source['signal_time'])) or
                    terms['lot_size'] != lot or amount(terms['tick']) != tick or amount(terms['stop']) != stop or
                    body['quantity'] <= 0 or body['quantity'] % lot or
                    body['quantity'] // lot > policy['limits']['max_lots'] or
                    not amount(signal['entry_low']) <= price <= amount(signal['entry_high']) or
                    len(targets) != 3 or not stop < price < targets[0] < targets[1] < targets[2] or
                    not tick or any(v % tick for v in [price, stop, *targets])):
                raise Refused('Entry does not match admitted signal')
            stop_limit = stop - tick * policy['limits']['stop_gap_ticks']
            if stop_limit <= 0:
                raise Refused('Reviewed stop gap exceeds trigger')
            values = (entry_tag, source['source'], source['digest'], policy['version'],
                      json.dumps([str(v) for v in targets]), str(stop_limit), policy['limits']['target_timeout_seconds'])
            old = self.db.execute('SELECT * FROM entry_exit_plans WHERE entry_tag=?', (entry_tag,)).fetchone()
            if old and tuple(old) != values:
                raise Refused('Bound entry exit plan cannot change')
            self.monitor.bind_stop_limit(entry_tag, stop_limit)
            self.db.execute('INSERT OR IGNORE INTO entry_exit_plans VALUES(?,?,?,?,?,?,?)', values)
            return dict(self.db.execute('SELECT * FROM entry_exit_plans WHERE entry_tag=?', (entry_tag,)).fetchone())


def handoff(monitor):
    """Return a strategy only when every exposed entry is final and has a plan.

    Called after reconciliation. No network call, BUY, lease change or policy
    reread: withdrawn consent must not abandon already-filled exposure. Missing
    plans leave the existing stop monitor in charge; legacy exposure is not guessed.
    """
    db = monitor.db
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='entry_exit_plans'").fetchone():
        return None
    if db.execute("SELECT 1 FROM intents WHERE status NOT IN ('FILLED','CANCELLED','REJECTED')").fetchone():
        return None
    exposed = db.execute('''SELECT i.* FROM intents i WHERE i.filled >
        (SELECT COALESCE(SUM(e.filled),0) FROM protective_exits e WHERE e.entry_tag=i.tag)''').fetchall()
    if not exposed:
        return None
    plans = [db.execute('SELECT * FROM entry_exit_plans WHERE entry_tag=?', (entry['tag'],)).fetchone() for entry in exposed]
    if any(plan is None for plan in plans):
        return None
    from .strategy import ExitStrategy
    manager = ExitStrategy(monitor.ledger, monitor.session, collect_trades=monitor.collect_trades)
    with monitor.ledger.transaction():
        for plan in plans:
            manager.bind(plan['entry_tag'], targets=json.loads(plan['targets']),
                         stop_limit=plan['stop_limit'], target_timeout=plan['target_timeout'])
    return manager
