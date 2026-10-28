"""Bounded, read-only chart of retained broker observations for one position."""
import json
from datetime import datetime, timedelta, timezone
from decimal import InvalidOperation
from ..core import stamp, dec


def chart(db, position, events, window='1D', now=None, quote_age=15):
    if window not in ('1D', '1W', '1M'):
        raise ValueError('Unknown chart window')
    now = now or datetime.now(timezone.utc)
    end = now
    exits = [e for e in events if e['event'] == 'PAPER_EXIT' or
             (e['event'] == 'TARGET_REACHED' and e['body'].get('remaining') == 0)]
    if position.get('status') == 'CLOSED' and exits:
        end = min(now, stamp(exits[-1]['at']))
    start = end - timedelta(days={'1D': 1, '1W': 7, '1M': 30}[window])
    # Never use a token's earlier history before this signal identified its contract.
    tracked = position.get('source_time') or position.get('entry_time')
    if tracked:
        start = max(start, stamp(tracked))
    contract = position.get('contract', {})
    points, truncated = [], False
    if contract.get('segment') and contract.get('token') is not None and db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='source_events'").fetchone():
        rows = db.execute(
            "SELECT received_at,body FROM source_events WHERE json_extract(body,'$.type')='quote' "
            "AND json_extract(body,'$.segment')=? AND CAST(json_extract(body,'$.token') AS TEXT)=? "
            "AND julianday(received_at)>=julianday(?) AND julianday(received_at)<=julianday(?) "
            "ORDER BY julianday(received_at) DESC,id DESC LIMIT 8001",
            (contract['segment'], str(contract['token']), start.isoformat(), end.isoformat())).fetchall()
        truncated = len(rows) > 8000
        for received, body in rows[:8000]:
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
                points.append(dict(at=at.isoformat(), bid=str(bid), ltp=str(ltp)))
            except (KeyError, ValueError, TypeError, InvalidOperation):
                continue
    points.sort(key=lambda p: stamp(p['at']))
    # Deduplicate timestamps deterministically; break connections at missing intervals.
    unique = {p['at']: p for p in points}
    points = list(unique.values())
    candles = {str(minutes): candle_buckets(points, minutes) for minutes in (3, 5, 10)}
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
        truncated = True
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
                observations=len(points), truncated=truncated, markers=markers, candles=candles,
                note='Retained broker observations since this signal, not full exchange history. '
                     '1D/1W/1M cover up to 24 hours/7 days/30 days ending now (or at final exit). '
                     'Lines break across gaps over 60 seconds. Sampled points preserve observed extrema. '
                     'LTP is last traded price; Orion values exits using bid. Telegram prices are excluded.')


def candle_buckets(points, minutes):
    """OHLC from validated raw observations, never from sampled line points.

    Buckets align to IST midnight. Empty intervals stay empty; coverage metadata
    identifies sparse/incomplete candles without inventing exchange ticks.
    """
    buckets = {}
    seconds = minutes * 60
    for point in points:
        at = stamp(point['at'])
        epoch = int(at.timestamp())
        start = ((epoch + 19800) // seconds) * seconds - 19800
        if start not in buckets:
            buckets[start] = dict(at=datetime.fromtimestamp(start, timezone.utc).isoformat(),
                                  first_at=point['at'], last_at=point['at'], count=0, max_gap_seconds=0,
                                  **{field: dict(open=point[field], high=point[field], low=point[field], close=point[field])
                                     for field in ('ltp', 'bid')})
        bucket = buckets[start]
        bucket['max_gap_seconds'] = max(bucket['max_gap_seconds'], (at - stamp(bucket['last_at'])).total_seconds())
        bucket['last_at'] = point['at']
        bucket['count'] += 1
        for field in ('ltp', 'bid'):
            values = bucket[field]
            values['high'] = str(max(dec(values['high']), dec(point[field])))
            values['low'] = str(min(dec(values['low']), dec(point[field])))
            values['close'] = point[field]
    for start, bucket in buckets.items():
        bucket['partial'] = (stamp(bucket['first_at']).timestamp() - start > 60 or
                             start + seconds - stamp(bucket['last_at']).timestamp() > 60 or
                             bucket['max_gap_seconds'] > 60 or bucket['count'] < 2)
    return list(buckets.values())
