"""Bounded subscriptions for active paper positions, independent of catalogue size."""


def active_tokens(positions):
    return {
        (p["contract"]["segment"], str(p["contract"]["token"]))
        for p in positions.values()
        if p["status"] in ("PENDING", "OPEN")
    }


class Subscriptions:
    def __init__(self, ws, token_factory, limit=100, batch_size=25):
        self.ws = ws
        self.token_factory = token_factory
        self.limit = limit
        self.batch_size = batch_size
        self.current = set()

    async def sync(self, positions):
        wanted = active_tokens(positions)
        if len(wanted) > self.limit:
            raise ValueError("Active contract subscription limit exceeded; review pending calls")
        removed = self.current - wanted
        for operation, keys in (
            (self.ws.unsubscribe_scrips, removed),
            (self.ws.subscribe_scrips, wanted - self.current),
        ):
            ordered = sorted(keys)
            for start in range(0, len(ordered), self.batch_size):
                batch = ordered[start : start + self.batch_size]
                await operation([self.token_factory(*key) for key in batch])
                if operation == self.ws.unsubscribe_scrips:
                    self.current.difference_update(batch)
                else:
                    self.current.update(batch)
        return removed
