"""One account cycle coordinates independent positions; no entry submission here."""
import json

from .ledger import Refused
from .monitor import ProtectionMonitor
from .strategy import ExitStrategy


class PortfolioMonitor(ProtectionMonitor):
    portfolio = True

    def __init__(self, ledger, session, *, collect_trades=False):
        super().__init__(ledger, session, collect_trades=collect_trades)
        self.exits = ExitStrategy(ledger, session, collect_trades=collect_trades)
        self._last = None

    def _bind_ready(self):
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='entry_exit_plans'").fetchone():
            return
        rows = self.db.execute('''SELECT p.* FROM entry_exit_plans p JOIN intents i ON i.tag=p.entry_tag
            WHERE i.status IN ('FILLED','CANCELLED') AND i.filled>0
            AND NOT EXISTS(SELECT 1 FROM strategy_plans s WHERE s.entry_tag=i.tag)''').fetchall()
        with self.ledger.transaction():
            for plan in rows:
                self.exits.bind(plan['entry_tag'], targets=json.loads(plan['targets']),
                                stop_limit=plan['stop_limit'], target_timeout=plan['target_timeout'])

    def cycle(self, *, entries_allowed=False, marks=None):
        try:
            snapshot = self.session.request('evidence' if self.collect_trades else 'snapshot')
            managed = {r[0] for r in self.db.execute('SELECT entry_tag FROM strategy_plans')}
            base = super().cycle(entries_allowed=entries_allowed, marks=marks or {},
                                 _snapshot=snapshot, _managed=managed)
            # Base handles manual requests and new/partial entries first. Never
            # issue a second command after its cancellation/protection action.
            if base['state'] == 'review-required' or base['action'] != 'none':
                return base
            self._bind_ready()
            tags = [r[0] for r in self.db.execute('''SELECT s.entry_tag FROM strategy_plans s JOIN intents i ON i.tag=s.entry_tag
                ORDER BY i.rowid''')]
            if not tags:
                return base
            # Rotate the first inspected position so an acknowledged target or
            # cancellation cannot indefinitely starve a later position's exits.
            if self._last in tags:
                index = (tags.index(self._last)+1) % len(tags)
                tags = tags[index:]+tags[:index]
            value = self.exits.cycle(marks=marks or {}, _snapshot=snapshot, _entries=tags)
            self._last = self.exits._last_processed
            if base['state'] != 'observed' and value['state'] == 'observed':
                return self._health(base['state'], base['action'])
            return value
        except Exception:
            self.ledger.pause()
            self._health('review-required', 'portfolio-cycle-failed')
            raise Refused('Portfolio monitoring requires review') from None
