"""Paper-feed diagnostics only; never infer or override permission to trade."""


class MarketEvidence:
    def __init__(self):
        self.clear()

    def clear(self):
        self.status = {}
        self.reported_missing = set()

    def observe(self, segment, code, observed_at):
        code = int(code)
        previous = self.status.get(segment)
        self.status[segment] = (code, observed_at)
        if previous is None or previous[0] != code:
            return dict(event="BROKER_MARKET_STATUS", segment=segment,
                        status_code=code, observed_at=observed_at,
                        market_open=code in (1, 4))

    def quote(self, segment):
        status = self.status.get(segment)
        warning = None
        if status is None and segment not in self.reported_missing:
            self.reported_missing.add(segment)
            warning = dict(event="BROKER_MARKET_STATUS_MISSING", segment=segment,
                           observed_status_segments=sorted(self.status))
        return dict(market_status_code=status[0] if status else None,
                    market_status_observed_at=status[1] if status else None,
                    market_status_known=status is not None), warning
