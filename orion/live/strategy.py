"""Persisted target/trailing coordinator for confirmed NSE option exposure.

No entry authority or production launcher. Each cycle reconciles before issuing
at most one command. Target limits and stop gaps are explicit; no market orders,
OCO assumption, cancel-ack capacity release, price chasing or automatic retry.
"""
from datetime import datetime, timezone
import json

from ..core import allocations
from .accounting import timestamp
from .ledger import Refused, amount
from .monitor import ProtectionMonitor
from .protection import check_conflicts

TERMINAL = ('FILLED', 'CANCELLED', 'REJECTED')


class ExitStrategy(ProtectionMonitor):
    def __init__(self, ledger, session, *, collect_trades=False):
        super().__init__(ledger, session, collect_trades=collect_trades)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS strategy_plans(
                entry_tag TEXT PRIMARY KEY, targets TEXT NOT NULL, quantities TEXT NOT NULL,
                stop_gap TEXT NOT NULL, target_timeout INTEGER NOT NULL,
                stage INTEGER NOT NULL DEFAULT 0, phase TEXT NOT NULL DEFAULT 'WATCH',
                active_exit TEXT, requested_at TEXT);
            CREATE TABLE IF NOT EXISTS strategy_cancellations(
                tag TEXT PRIMARY KEY, entry_tag TEXT NOT NULL);
        ''')

    def bind(self, entry_tag, *, targets, stop_limit, target_timeout):
        """Bind once to final filled entry units; partial working entries wait.

        Three lots allocate one per target; one lot exits at T3; two lots at T1/T3.
        Stops trail to the entry limit at T1 and T1 at T2. For a zero-unit target,
        reaching that price advances the stop; otherwise confirmed target fills do.
        """
        entry = self.ledger.get(entry_tag)
        body = json.loads(entry['body'])
        terms = self.db.execute('SELECT * FROM execution_terms WHERE tag=?', (entry_tag,)).fetchone()
        if not terms or entry['status'] not in ('FILLED','CANCELLED') or not entry['filled']:
            raise Refused('Final confirmed entry fills required')
        if entry['filled'] % terms['lot_size']:
            raise Refused('Whole-lot confirmed exposure required')
        if type(target_timeout) is not int or not 1 <= target_timeout <= 60:
            raise Refused('Explicit bounded target timeout required')
        if not isinstance(targets, (list, tuple)) or len(targets) != 3:
            raise Refused('Three targets required')
        prices = [amount(p) for p in targets]
        stop, tick, limit = amount(terms['stop']), amount(terms['tick']), amount(stop_limit)
        if not amount(body['limit_price']) < prices[0] < prices[1] < prices[2]:
            raise Refused('Targets must rise above entry limit')
        if not 0 < limit <= stop or limit % tick or any(p % tick for p in prices):
            raise Refused('Tick-aligned target and stop-limit policy required')
        quantities = [n*terms['lot_size'] for n in allocations(entry['filled']//terms['lot_size'])]
        values = (entry_tag, json.dumps([str(p) for p in prices]), json.dumps(quantities), str(stop-limit), target_timeout)
        with self.ledger.transaction():
            old = self.db.execute('SELECT entry_tag,targets,quantities,stop_gap,target_timeout FROM strategy_plans WHERE entry_tag=?', (entry_tag,)).fetchone()
            if old and tuple(old) != values:
                raise Refused('Exit policy cannot change')
            self.db.execute('INSERT OR IGNORE INTO strategy_plans(entry_tag,targets,quantities,stop_gap,target_timeout) VALUES(?,?,?,?,?)', values)
            self.ledger.pause()

    def _cancel(self, kind, row):
        prior = self.db.execute("SELECT 1 FROM broker_commands WHERE kind=? AND tag=? AND operation='cancel'", (kind, row['tag'])).fetchone()
        if prior:
            return self._health('awaiting-broker', 'cancel-confirmation-pending')
        with self.ledger.transaction():
            self.db.execute('INSERT OR IGNORE INTO strategy_cancellations VALUES(?,?)', (row['tag'], row['entry_tag']))
        self.commands.cancel(kind, row['tag'])
        return self._health('awaiting-broker', 'strategy-cancel-requested')

    def _stop(self, entry, plan, terms):
        if plan['stage'] == 0:
            return amount(terms['stop'])
        if plan['stage'] == 1:
            return amount(json.loads(entry['body'])['limit_price'])
        return amount(json.loads(plan['targets'])[0])

    def _protect(self, entry, plan, terms, stops):
        trigger = self._stop(entry, plan, terms)
        # All old stop capacity stays reserved until terminal broker observation.
        for row in stops:
            if row['status'] in ('OPEN','PARTIAL') and amount(row['trigger']) != trigger:
                return self._cancel('STOP', row)
        free = self.protection._unreserved(entry['tag'])
        if not free:
            return None
        if free % terms['lot_size']:
            self.protection.incident('strategy-requires-review')
            return self._health('review-required', 'odd-lot-exposure')
        row = self.protection.prepare(entry['tag'], trigger_price=trigger, tick_size=terms['tick'], lot_size=terms['lot_size'])
        self.protection.dispatch(row['tag'])
        self.commands.place('STOP', row['tag'], stop_limit=str(trigger-amount(plan['stop_gap'])))
        return self._health('awaiting-broker', 'strategy-protection-requested')

    def cycle(self, *, marks):
        """Marks are explicit test/integration inputs, not trusted production quotes.

        Stale quotes cannot trigger targets or trailing moves. Existing exits are
        still observed and target timeouts can cancel outstanding unprotected sells.
        A stop-limit gap is escalated; this class never chooses a new market price.
        """
        try:
            snapshot = self.session.request('evidence' if self.collect_trades else 'snapshot')
            if self.collect_trades:
                self.observations.ingest_evidence(snapshot)
            else:
                self.observations.ingest_snapshot(snapshot)
            now = datetime.now(timezone.utc)
            self.ledger.pause()
            check_conflicts(self.db)
            # Unknown commands cannot authorize replacement capacity after restart.
            if self.db.execute("SELECT 1 FROM broker_commands WHERE status IN ('SENDING','UNKNOWN')").fetchone():
                return self._health('review-required', 'unknown-command')
            for entry in self.db.execute('SELECT * FROM intents ORDER BY rowid').fetchall():
                if entry['status'] in ('OPEN','PARTIAL'):
                    # The base monitor handles partial-entry protection. Do not
                    # overlap coordinators or bind a strategy to changing sizing.
                    return self._health('review-required', 'entry-not-final')
                if not entry['filled']:
                    continue
                if not self.protection._remaining(entry['tag']):
                    if self.db.execute("SELECT 1 FROM protective_exits WHERE entry_tag=? AND quantity>filled AND status NOT IN ('FILLED','CANCELLED','REJECTED')", (entry['tag'],)).fetchone():
                        self.protection.incident('strategy-requires-review')
                        return self._health('review-required', 'working-sell-without-exposure')
                    self.db.execute("UPDATE strategy_plans SET phase='DONE',active_exit=NULL,requested_at=NULL WHERE entry_tag=?", (entry['tag'],))
                    continue
                plan = self.db.execute('SELECT * FROM strategy_plans WHERE entry_tag=?', (entry['tag'],)).fetchone()
                terms = self.db.execute('SELECT * FROM execution_terms WHERE tag=?', (entry['tag'],)).fetchone()
                if not plan or not terms:
                    return self._health('review-required', 'exit-policy-required')
                exits = self.db.execute('SELECT * FROM protective_exits WHERE entry_tag=?', (entry['tag'],)).fetchall()
                if any(r['status'] in ('PREPARED','DISPATCHING','UNKNOWN','CANCEL_PENDING') for r in exits):
                    return self._health('review-required', 'unresolved-exit')
                stops = [r for r in exits if amount(r['trigger']) > 0]
                if any(r['status'] == 'REJECTED' for r in stops):
                    return self._health('review-required', 'rejected-stop')
                if self.db.execute("SELECT 1 FROM live_incidents WHERE code='unprotected-exposure'").fetchone():
                    return self._health('review-required', 'unexpected-exit-cancellation')
                quote = marks.get(entry['tag'])
                fresh = False
                if isinstance(quote, tuple) and len(quote) == 2:
                    fresh = 0 <= (now-timestamp(quote[1])).total_seconds() <= 30
                    price = amount(quote[0])
                    fresh = fresh and price > 0
                if fresh and any(r['status'] in ('OPEN','PARTIAL') and price <= amount(r['trigger'])-amount(plan['stop_gap']) for r in stops):
                    self.protection.incident('strategy-requires-review')
                    return self._health('review-required', 'stop-limit-gap')
                if plan['active_exit']:
                    target = self.protection.get(plan['active_exit'])
                    if target['status'] in ('OPEN','PARTIAL'):
                        if (now-datetime.fromisoformat(plan['requested_at'])).total_seconds() >= plan['target_timeout']:
                            return self._cancel('EXIT', target)
                        action = self._protect(entry, plan, terms, stops)
                        return action or self._health('awaiting-broker', 'target-working')
                    if target['status'] == 'FILLED':
                        with self.ledger.transaction():
                            self.db.execute("UPDATE strategy_plans SET stage=stage+1,phase='WATCH',active_exit=NULL,requested_at=NULL WHERE entry_tag=?", (entry['tag'],))
                        return self._health('awaiting-broker', 'target-fill-confirmed')
                    if target['status'] in ('CANCELLED','REJECTED'):
                        with self.ledger.transaction():
                            self.db.execute("UPDATE strategy_plans SET phase='HALTED',active_exit=NULL WHERE entry_tag=?", (entry['tag'],))
                            self.protection.incident('strategy-requires-review')
                        return self._health('review-required', 'target-ended-before-completion')
                if plan['phase'] == 'HALTED' or self.db.execute("SELECT 1 FROM live_incidents WHERE code='strategy-requires-review'").fetchone():
                    action = self._protect(entry, plan, terms, stops)
                    return action or self._health('review-required', 'strategy-halted')
                stage = plan['stage']
                if stage >= 3:
                    return self._health('review-required', 'unexpected-residual-exposure')
                target_price = amount(json.loads(plan['targets'])[stage])
                if plan['phase'] == 'CANCEL':
                    # Revalidate before every cancellation, not only before the
                    # target send. Preserve remaining stops if the quote is lost.
                    if not fresh or price < target_price:
                        self.db.execute("UPDATE strategy_plans SET phase='WATCH' WHERE entry_tag=?", (entry['tag'],))
                        action = self._protect(entry, plan, terms, stops)
                        return action or self._health('awaiting-broker', 'target-no-longer-current')
                    for row in stops:
                        if row['status'] in ('OPEN','PARTIAL'):
                            return self._cancel('STOP', row)
                    quantity = min(json.loads(plan['quantities'])[stage], self.protection._unreserved(entry['tag']))
                    if not quantity or quantity % terms['lot_size']:
                        self.protection.incident('strategy-requires-review')
                        return self._health('review-required', 'target-capacity-changed')
                    with self.ledger.transaction():
                        row = self.protection.prepare_limit(entry['tag'], quantity=quantity, limit_price=target_price,
                                                            tick_size=terms['tick'], lot_size=terms['lot_size'])
                        self.protection.dispatch(row['tag'])
                        self.db.execute("UPDATE strategy_plans SET phase='TARGET',active_exit=?,requested_at=? WHERE entry_tag=?",
                                        (row['tag'], now.isoformat(), entry['tag']))
                    self.commands.place('EXIT', row['tag'])
                    return self._health('awaiting-broker', 'target-requested')
                action = self._protect(entry, plan, terms, stops)
                if action:
                    return action
                if not fresh:
                    return self._health('review-required', 'quote-stale')
                if price >= target_price:
                    if json.loads(plan['quantities'])[stage] == 0:
                        self.db.execute('UPDATE strategy_plans SET stage=stage+1 WHERE entry_tag=?', (entry['tag'],))
                        return self._health('awaiting-broker', 'trailing-milestone')
                    self.db.execute("UPDATE strategy_plans SET phase='CANCEL' WHERE entry_tag=?", (entry['tag'],))
                    return self._health('awaiting-broker', 'target-reached')
            return self._health('observed', 'none')
        except Exception:
            self.ledger.pause()
            self._health('review-required', 'strategy-cycle-failed')
            raise Refused('Exit strategy requires review') from None
