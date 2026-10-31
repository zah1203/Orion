"""Bounded, read-only chart of retained broker observations for one position."""
import json
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import InvalidOperation
from ..core import stamp, dec


def chart(db, position, events, window='1D', now=None, quote_age=15):
    if window not in ('DAY', '1D', '1W', '1M'):
        raise ValueError('Unknown chart window')
    now = now or datetime.now(timezone.utc)
    end = now
    exits = [e for e in events if e['event'] == 'PAPER_EXIT' or
             (e['event'] == 'TARGET_REACHED' and e['body'].get('remaining') == 0)]
    if position.get('status') == 'CLOSED' and exits:
        end = min(now, stamp(exits[-1]['at']))
    ist = timezone(timedelta(hours=5, minutes=30))
    start = (end.astimezone(ist).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
             if window == 'DAY' else end - timedelta(days={'1D': 1, '1W': 7, '1M': 30}[window]))
    # Never use a token's earlier history before this signal identified its contract.
    tracked = position.get('source_time') or position.get('entry_time')
    if tracked:
        start = max(start, stamp(tracked))
    contract = position.get('contract', {})
    points = deque(maxlen=8000)  # Legacy line preview only; candles consume every valid quote.
    accumulators = {str(m): CandleAccumulator(m) for m in (3, 5, 10, 15, 30, 60)}
    observations, first_at, last_at = 0, None, None
    if contract.get('segment') and contract.get('token') is not None and db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='source_events'").fetchone():
        rows = db.execute(
            "SELECT received_at,body FROM source_events WHERE json_extract(body,'$.type')='quote' "
            "AND json_extract(body,'$.segment')=? AND CAST(json_extract(body,'$.token') AS TEXT)=? "
            "AND julianday(received_at)>=julianday(?) AND julianday(received_at)<=julianday(?) "
            "ORDER BY julianday(json_extract(body,'$.source_time')),julianday(received_at) DESC,id DESC",
            (contract['segment'], str(contract['token']), start.isoformat(), end.isoformat()))
        for received, body in rows:
            try:
                quote = json.loads(body)
                at = stamp(quote['source_time'])
                bid, ask, ltp = (dec(quote[k]) for k in ('bid', 'ask', 'ltp'))
                if not all(v.is_finite() for v in (bid, ask, ltp)) or not (0 < bid <= ask and ltp > 0):
                    continue
                if not -2 <= (stamp(received) - at).total_seconds() <= quote_age:
                    continue
                if not start <= at <= end or not quote.get('market_open'):
                    continue
                point = dict(at=at.isoformat(), bid=str(bid), ltp=str(ltp))
                if point['at'] == last_at:
                    continue
                first_at = first_at or point['at']
                last_at = point['at']
                observations += 1
                points.append(point)
                for accumulator in accumulators.values():
                    accumulator.add(point)
            except (KeyError, ValueError, TypeError, InvalidOperation):
                continue
    points = list(points)
    candles = {key: value.finish() for key, value in accumulators.items()}
    line_truncated = observations > len(points)
    segments, segment = [], []
    for point in points:
        if segment and (stamp(point['at']) - stamp(segment[-1]['at'])).total_seconds() > 60:
            segments.append(segment)
            segment = []
        segment.append(point)
    if segment:
        segments.append(segment)
    # At most 8000 valid observations, reduced by fixed buckets preserving both
    # series' extrema and the endpoints. Segment boundaries are never joined.
    if len(segments) > 240:
        segments = segments[-240:]
        line_truncated = True
    size = max(1, (len(points) + 119) // 120)
    sampled = []
    for segment in segments:
        selected = []
        for offset in range(0, len(segment), size):
            bucket = segment[offset:offset + size]
            keep = {0, len(bucket) - 1}
            for field in ('bid', 'ltp'):
                keep.add(min(range(len(bucket)), key=lambda i: dec(bucket[i][field])))
                keep.add(max(range(len(bucket)), key=lambda i: dec(bucket[i][field])))
            selected.extend(bucket[i] for i in sorted(keep))
        sampled.append(selected)
    markers = [dict(at=e['at'], price=e['body']['price'],
                    label='Entry' if e['event'] == 'PAPER_BUY' else
                          'T' + str(e['body'].get('target', '?')) if e['event'] == 'TARGET_REACHED' else
                          'Exit: ' + e['body'].get('reason', 'closed'))
               for e in events if e['event'] in ('PAPER_BUY', 'TARGET_REACHED', 'PAPER_EXIT')
               and e['body'].get('price') is not None and start <= stamp(e['at']) <= end]
    return dict(window=window, start=start.isoformat(), end=end.isoformat(), segments=sampled,
                observations=observations, truncated=False, line_truncated=line_truncated,
                first_at=first_at, last_at=last_at, latest=points[-1] if points else None,
                markers=markers, candles=candles,
                note='Retained broker observations since this signal, not full exchange history. '
                     '1D/1W/1M cover up to 24 hours/7 days/30 days ending now (or at final exit). '
                     'Lines break across gaps over 60 seconds. Sampled points preserve observed extrema. '
                     'LTP is last traded price; Orion values exits using bid. Telegram prices are excluded.')


class CandleAccumulator:
    """One OHLC bucket per interval, not one in-memory object per quote."""
    def __init__(self, minutes):
        self.seconds = minutes * 60
        self.buckets = {}

    def add(self, point):
        at = stamp(point['at'])
        start = ((int(at.timestamp()) + 19800) // self.seconds) * self.seconds - 19800
        if start not in self.buckets:
            self.buckets[start] = dict(at=datetime.fromtimestamp(start, timezone.utc).isoformat(),
                                      first_at=point['at'], last_at=point['at'], count=0, max_gap_seconds=0,
                                      **{field: dict(open=point[field], high=point[field], low=point[field], close=point[field])
                                         for field in ('ltp', 'bid')})
        bucket = self.buckets[start]
        bucket['max_gap_seconds'] = max(bucket['max_gap_seconds'], (at - stamp(bucket['last_at'])).total_seconds())
        bucket['last_at'] = point['at']
        bucket['count'] += 1
        for field in ('ltp', 'bid'):
            values = bucket[field]
            values['high'] = str(max(dec(values['high']), dec(point[field])))
            values['low'] = str(min(dec(values['low']), dec(point[field])))
            values['close'] = point[field]

    def finish(self):
        for start, bucket in self.buckets.items():
            bucket['partial'] = (stamp(bucket['first_at']).timestamp() - start > 60 or
                                 start + self.seconds - stamp(bucket['last_at']).timestamp() > 60 or
                                 bucket['max_gap_seconds'] > 60 or bucket['count'] < 2)
        return list(self.buckets.values())


def candle_buckets(points, minutes):
    accumulator = CandleAccumulator(minutes)
    for point in points:
        accumulator.add(point)
    return accumulator.finish()
