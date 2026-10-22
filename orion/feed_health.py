"""Track usable paper quotes per subscription without trusting socket liveness."""

import time


class FeedHealth:
    def __init__(self, max_age, clock=time.monotonic):
        self.max_age = max_age
        self.reconnect_after = max(60, 3 * max_age)
        self.clock = clock
        self.last = {}

    def sync(self, tokens, market_status):
        # A known closed exchange must not exhaust retries overnight. Unknown
        # status still needs recovery: a lost status subscription is not healthy.
        watched = {key for key in tokens if market_status.get(key[0]) is not False}
        now = self.clock()
        self.last = {key: self.last.get(key, now) for key in watched}

    def accepted(self, key, results):
        if any(r['event'] in ('STALE_QUOTE', 'OUT_OF_ORDER_QUOTE', 'INVALID_QUOTE')
               for r in results):
            return False
        self.last[key] = self.clock()
        return True

    def overdue(self, *, reconnect=False):
        age = self.reconnect_after if reconnect else self.max_age
        now = self.clock()
        return sorted(key for key, seen in self.last.items() if now - seen > age)
