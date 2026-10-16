"""Bounded fresh market-status notification from the authenticated SFeed session.

No clock-based inference, REST-price inference, reconnect or cached-open fallback.
An absent status notification is unavailable, not evidence that trading is open.
"""
import asyncio
from datetime import datetime, timezone

from .ledger import Refused


async def observe(client, segment='nse_fo'):
    if segment not in ('nse_fo', 'mcx_fo'):
        raise Refused('Explicit supported derivatives segment required')
    from neo_api_client.websocket.feed import SFeedMarketStatus
    async with asyncio.timeout(3):
        async with client.create_websocket(max_connect_retries=0, max_reconnect_attempts=0,
                                           ack_wait_timeout=2, verify_ssl=True) as ws:
            await ws.subscribe_exchange()
            async for event in ws:
                if isinstance(event, SFeedMarketStatus) and event.exchange_segment == segment:
                    return dict(segment=segment, status_code=event.status_code,
                                observed_at=datetime.now(timezone.utc).isoformat())
    raise Refused('Fresh segment-bound market status unavailable')


def read(client, segment='nse_fo'):
    try:
        return asyncio.run(observe(client, segment=segment))
    except Exception:
        raise Refused('Fresh segment-bound market status unavailable') from None
