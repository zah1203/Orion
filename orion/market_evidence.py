"""Paper-feed status transitions and retained broker evidence."""


class MarketEvidence:
    def __init__(self):
        self.clear()

    def clear(self):
        self.status = {}
        self.open_state = {}
        self.reported_missing = set()

    def observe(self, segment, code, observed_at):
        code = int(code)
        previous = self.status.get(segment)
        self.status[segment] = (code, observed_at)
        # CAS end is an auction notification, not an independent normal-market
        # open/close transition. Preserve only this connection's NSE FO state.
        # All other segments/codes retain the existing fail-closed policy.
        if not (segment == "nse_fo" and code == 12):
            self.open_state[segment] = code in (1, 4)
        if previous is None or previous[0] != code:
            return dict(event="BROKER_MARKET_STATUS", segment=segment,
                        status_code=code, observed_at=observed_at,
                        market_open=self.is_open(segment),
                        status_action="preserve" if segment == "nse_fo" and code == 12 else "replace")

    def is_open(self, segment):
        return self.open_state.get(segment, False)

    def quote(self, segment):
        status = self.status.get(segment)
        warning = None
        if status is None and segment not in self.reported_missing:
            self.reported_missing.add(segment)
            warning = dict(event="BROKER_MARKET_STATUS_MISSING", segment=segment,
                           observed_status_segments=sorted(self.status))
        return dict(market_status_effective_open=self.is_open(segment),
                    market_status_code=status[0] if status else None,
                    market_status_observed_at=status[1] if status else None,
                    market_status_known=status is not None), warning
