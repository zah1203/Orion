"""Decimal paper P&L, marked at the last accepted option bid."""

from datetime import datetime, timezone
from decimal import Decimal
from ..core import dec, stamp


def report(state, max_age_seconds, now=None):
    now = now or datetime.now(timezone.utc)
    positions = []
    groups = {}
    marks = state.get("marks", {})
    for signal_id, p in state.get("positions", {}).items():
        if not p.get("entry_fill") or not p.get("lots"):
            continue
        c = p["contract"]
        remaining = p["remaining"]
        realized = dec(p["pnl"])
        unrealized = Decimal(0)
        value = Decimal(0)
        status = "closed"
        mark = marks.get(c["segment"] + ":" + str(c["token"])) if remaining else None
        if remaining:
            if mark:
                age = (now - stamp(mark["source_time"])).total_seconds()
                status = "fresh" if 0 <= age <= max_age_seconds and mark.get("market_open") else "stale"
                value = dec(mark["bid"]) * dec(c["premium_multiplier"]) * remaining
                unrealized = (
                    (dec(mark["bid"]) - dec(p["entry_fill"])) * dec(c["premium_multiplier"]) * remaining
                )
            else:
                status = "unavailable"
                unrealized = value = None
        row = dict(
            signal_id=signal_id,
            product=c["product"],
            symbol=c["symbol"],
            expiry=c["expiry"],
            strike=c["strike"],
            option_type=c["option_type"],
            status=p["status"],
            lots=p["lots"],
            remaining=remaining,
            entry=p["entry_fill"],
            stop=p["stop"],
            realized=str(realized),
            unrealized=None if unrealized is None else str(unrealized),
            total=None if unrealized is None else str(realized + unrealized),
            mark_status=status,
            bid=mark["bid"] if mark else None,
            quote_time=mark["source_time"] if mark else None,
        )
        positions.append(row)
        group = groups.setdefault(
            c["product"],
            dict(
                product=c["product"],
                realized=Decimal(0),
                unrealized=Decimal(0),
                market_value=Decimal(0),
                trades=0,
                open_lots=0,
                stale_positions=0,
                missing_positions=0,
            ),
        )
        group["realized"] += realized
        group["trades"] += 1
        group["open_lots"] += remaining
        group["stale_positions"] += status == "stale"
        group["missing_positions"] += status == "unavailable"
        if unrealized is not None:
            group["unrealized"] += unrealized
            group["market_value"] += value
    totals = dict(
        realized=Decimal(0),
        unrealized=Decimal(0),
        market_value=Decimal(0),
        trades=0,
        open_lots=0,
        stale_positions=0,
        missing_positions=0,
    )
    for group in groups.values():
        for key in totals:
            totals[key] += group[key]

    def serialize(group):
        out = dict(group)
        missing = group["missing_positions"] > 0
        out["total"] = None if missing else str(group["realized"] + group["unrealized"])
        out["realized"] = str(group["realized"])
        out["unrealized"] = None if missing else str(group["unrealized"])
        out["market_value"] = None if missing else str(group["market_value"])
        out["mark_status"] = "unavailable" if missing else ("stale" if group["stale_positions"] else "fresh")
        return out

    total = serialize(totals)
    total["cash"] = str(state["cash"])
    total["equity"] = (
        None if totals["missing_positions"] else str(dec(state["cash"]) + totals["market_value"])
    )
    return dict(
        as_of=now.isoformat(),
        period="Since account started",
        totals=total,
        instruments=[serialize(groups[k]) for k in sorted(groups)],
        positions=positions,
        note="Paper values only. Realized P&L includes charged simulation fees. Open P&L uses the last accepted bid and excludes future exit fees. Stale values are last-known estimates, not current quotes.",
    )
