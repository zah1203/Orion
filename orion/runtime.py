"""Live Telegram + Kotak inputs; all order fills remain simulated."""

import asyncio
import getpass
import hashlib
import json
import os
from pathlib import Path
import uuid
from datetime import datetime, timezone
from .core import Engine, IST, dumps


def credentials():
    # Values never enter Terraform, command-line arguments, or logs.
    import boto3

    value = boto3.client(
        "secretsmanager", region_name=os.environ.get("AWS_REGION", "ap-south-1")
    ).get_secret_value(SecretId=os.environ["ORION_SECRET_ARN"])
    return json.loads(value["SecretString"])


def telegram_login(session_path):
    from telethon.sync import TelegramClient

    os.umask(0o077)
    creds = credentials()
    with TelegramClient(session_path, int(creds["telegram_api_id"]), creds["telegram_api_hash"]) as client:
        # Telethon prompts locally for login code and optional 2FA. Never send messages.
        print("Telegram session authorized:", client.is_user_authorized())


def read_totp():
    path = os.environ.get("ORION_TOTP_FILE")
    if path:
        p = Path(path)
        code = p.read_text().strip()
        p.unlink()
    else:
        code = getpass.getpass("Current Kotak TOTP: ")
    if len(code) != 6 or not code.isdigit():
        raise ValueError("Expected six-digit TOTP")
    return code


async def serve(config, master, db_path, session_path):
    from telethon import TelegramClient, events
    from neo_api_client import NeoAPI
    from neo_api_client.websocket.feed import WsToken, SFeedScrip, SFeedMarketStatus

    os.umask(0o077)
    if master.get("synthetic") or master["as_of"] != datetime.now(IST).date().isoformat():
        raise ValueError("Current, verified instrument master required")
    if not config.get("live_inputs_enabled", False):
        raise ValueError("Set live_inputs_enabled only after configuring channel IDs and contracts")
    engine = Engine(db_path, config, master)
    engine.db.execute(
        "CREATE TABLE IF NOT EXISTS source_events(id TEXT PRIMARY KEY, received_at TEXT, body TEXT)"
    )
    # Reset crossing observations on restart: do not bridge unobserved outages.
    with engine.db:
        s = engine.state()
        for p in s["positions"].values():
            if p["status"] == "PENDING":
                p["status"] = "CANCELLED"
        engine.db.execute("UPDATE state SET body=? WHERE id=1", (dumps(s),))
    creds = credentials()
    client = NeoAPI(consumer_key=creds["kotak_consumer_key"], environment="prod")
    client.totp_login(mobile_number=creds["kotak_mobile"], ucc=creds["kotak_ucc"], totp=read_totp())
    client.totp_validate(mpin=creds["kotak_mpin"])
    telegram = TelegramClient(session_path, int(creds["telegram_api_id"]), creds["telegram_api_hash"])
    await telegram.connect()
    if not await telegram.is_user_authorized():
        raise ValueError("Run telegram-login interactively first")
    boot = datetime.now(timezone.utc)
    last_quote = {}
    market_status = {}

    def ingest(event):
        now = datetime.now(timezone.utc)
        with engine.db:
            engine.db.execute(
                "INSERT OR IGNORE INTO source_events VALUES(?,?,?)",
                (event["event_id"], now.isoformat(), dumps(event)),
            )
        for result in engine.process(event, now):
            print(dumps(result), flush=True)

    async def message(event):
        if event.message.date < boot and not event.message.edit_date:
            return  # never replay old Telegram backlog as new entries
        text = event.raw_text or ""
        edited = event.message.edit_date is not None
        digest = hashlib.sha256(text.encode()).hexdigest()
        ingest(
            dict(
                type="message",
                event_id=f"tg:{event.chat_id}:{event.id}:{edited}:{digest}",
                channel_id=str(event.chat_id),
                message_id=str(event.id),
                source_time=event.message.date.isoformat(),
                edited=edited,
                text=text,
                reply_to=event.message.reply_to_msg_id,
            )
        )

    channels = [int(x) for x in config["channels"]]
    telegram.add_event_handler(message, events.NewMessage(chats=channels))
    telegram.add_event_handler(message, events.MessageEdited(chats=channels))

    async def watchdog():
        while True:
            await asyncio.sleep(15)
            now = datetime.now(timezone.utc)
            for p in engine.state()["positions"].values():
                if p["status"] not in ("OPEN", "PENDING"):
                    continue
                k = p["contract"]["segment"] + ":" + str(p["contract"]["token"])
                if (
                    k not in last_quote
                    or (now - last_quote[k]).total_seconds() > config["quote_max_age_seconds"]
                ):
                    print(dumps({"event": "FEED_STALE", "contract": p["contract"]["symbol"]}), flush=True)

    async def feed():
        async with client.create_websocket() as ws:
            await ws.subscribe_exchange()
            await ws.subscribe_scrips([WsToken(c["segment"], str(c["token"])) for c in master["contracts"]])
            async for m in ws:
                if isinstance(m, SFeedMarketStatus):
                    market_status[m.exchange_segment] = int(m.status_code) in (1, 4)
                elif isinstance(m, SFeedScrip):
                    bids = [x.price for x in m.buy if x.quantity > 0 and x.price > 0]
                    asks = [x.price for x in m.sell if x.quantity > 0 and x.price > 0]
                    if not bids or not asks or m.auction:
                        continue
                    now = datetime.now(timezone.utc)
                    # Explicit unit setting. An unknown epoch fails freshness, never becomes 'now'.
                    divisor = 1000 if config["feed_timestamp_unit"] == "milliseconds" else 1
                    try:
                        source = datetime.fromtimestamp(m.last_update_time / divisor, timezone.utc)
                    except (ValueError, OverflowError, OSError):
                        continue
                    if abs((now - source).total_seconds()) > config["quote_max_age_seconds"]:
                        continue
                    k = m.exchange_segment + ":" + str(m.instrument_token)
                    # After a gap, invalidate pending crossings before accepting the next tick.
                    if (
                        k in last_quote
                        and (now - last_quote[k]).total_seconds() > config["quote_max_age_seconds"]
                    ):
                        with engine.db:
                            s = engine.state()
                            for p in s["positions"].values():
                                if (
                                    p["status"] == "PENDING"
                                    and p["contract"]["segment"] + ":" + str(p["contract"]["token"]) == k
                                ):
                                    p["status"] = "CANCELLED"
                            engine.db.execute("UPDATE state SET body=? WHERE id=1", (dumps(s),))
                    last_quote[k] = now
                    ingest(
                        dict(
                            type="quote",
                            event_id=str(uuid.uuid4()),
                            source_time=source.isoformat(),
                            segment=m.exchange_segment,
                            token=str(m.instrument_token),
                            ltp=str(m.last_traded_price),
                            bid=str(max(bids)),
                            ask=str(min(asks)),
                            volume=m.volume_traded_today,
                            open_interest=m.open_interest,
                            market_open=market_status.get(m.exchange_segment, False),
                        )
                    )
        raise RuntimeError("Market feed ended; inspect and authenticate before restarting")

    async def telegram_loop():
        await telegram.run_until_disconnected()
        raise RuntimeError("Telegram disconnected; inspect before restarting")

    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(telegram_loop())
            group.create_task(watchdog())
            group.create_task(feed())
    finally:
        await telegram.disconnect()
        client.logout()
        engine.db.close()
