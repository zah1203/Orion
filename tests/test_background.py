import asyncio
from contextlib import redirect_stdout
from datetime import datetime, timezone
import fcntl
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from orion.portal.app import defaults
from orion.portal.background import Background
from orion.portal.broker_session import load, save, validate
from orion.portal.connections import Connections
from orion.portal.store import Store
from orion.runtime import serve


class Sessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key = Fernet.generate_key()
        self.store = Store(self.tmp.name, self.key)
        self.uid = self.store.create_user("alice", "long test password", defaults())
        self.creds = dict(
            kotak_consumer_key="KEY", kotak_mobile="+919999999999", kotak_ucc="UCC", kotak_mpin="654321"
        )
        self.store.save_credentials(self.uid, self.creds)
        self.values = dict(edit_token="SECRET-TOKEN", edit_sid="SECRET-SID", ucc="UCC")

    def test_encrypted_reuse_expiry_and_credential_binding(self):
        save(self.store, self.uid, self.creds, self.values)
        with self.store.db() as db:
            ciphertext = db.execute("SELECT ciphertext FROM broker_sessions").fetchone()[0]
        self.assertNotIn(b"SECRET", ciphertext)
        reopened = Store(self.tmp.name, self.key)
        self.assertEqual(load(reopened, self.uid)["values"], self.values)
        with patch(
            "orion.portal.broker_session.time.time", return_value=load(self.store, self.uid)["expires"]
        ):
            self.assertIsNone(load(self.store, self.uid))
        other = self.store.create_user("bob", "long test password", defaults())
        self.assertIsNone(load(self.store, other))
        with self.store.db() as db:
            db.execute("INSERT INTO broker_sessions VALUES(?,?)", (other, ciphertext))
        with self.assertRaisesRegex(ValueError, "ownership"):
            load(self.store, other)
        self.store.save_credentials(self.uid, {"kotak_consumer_key": "NEW"})
        self.assertIsNone(load(self.store, self.uid))
        self.store.save_credentials(self.uid, self.creds)
        self.assertIsNone(load(self.store, self.uid))

    def test_reject_credential_exfiltration_and_extra_secret_fields(self):
        for extra in (
            {"feed_url": "wss://evil.example/feed"},
            {"totp": "123456"},
            {"feed_url": "ws://sfeed.kotaksecurities.com/feed"},
        ):
            with self.assertRaises(ValueError):
                validate({**self.values, **extra})

    def test_catalogue_rollover_and_atomic_reload(self):
        path = Path(self.tmp.name) / "contracts.json"
        bg = Background(self.store, self.uid, path)
        self.assertFalse(bg.catalogue()[1])
        from orion.core import IST

        path.write_text(
            json.dumps({"synthetic": False, "as_of": datetime.now(IST).date().isoformat(), "contracts": [{}]})
        )
        self.assertTrue(bg.catalogue()[1])
        replacement = path.with_suffix(".new")
        replacement.write_text("{broken")
        replacement.replace(path)
        self.assertFalse(bg.catalogue()[1])


class RunningWorker(unittest.IsolatedAsyncioTestCase):
    async def test_both_channels_record_without_broker_and_no_replay_on_login(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root, Fernet.generate_key())
            cfg = defaults()
            cfg["live_inputs_enabled"] = True
            cfg["channels"] = {
                "-100111": {"products": ["NIFTY"], "exit_time_ist": "15:15"},
                "-100222": {"products": ["GOLDM"], "exit_time_ist": "22:45"},
            }
            uid = store.create_user("alice", "long test password", cfg)
            store.set_enabled(uid, True)
            creds = dict(telegram_api_id="1234", telegram_api_hash="HASH", kotak_consumer_key="KEY")
            store.save_credentials(uid, creds)
            bg = Background(store, uid, Path(root) / "missing.json")
            paper = str(store.account_dir(uid) / "paper.db")
            handlers = []
            ready = asyncio.Event()

            class Telegram:
                async def connect(self):
                    pass

                async def disconnect(self):
                    pass

                def is_connected(self):
                    return True

                async def is_user_authorized(self):
                    return True

                def add_event_handler(self, fn, event):
                    handlers.append((fn, event))

                async def run_until_disconnected(self):
                    ready.set()
                    await asyncio.Future()

            def event(channel, mid, text):
                return SimpleNamespace(
                    chat_id=channel,
                    id=mid,
                    raw_text=text,
                    message=SimpleNamespace(
                        date=datetime.now(timezone.utc), edit_date=None, reply_to_msg_id=None
                    ),
                )

            with (
                patch("telethon.TelegramClient", return_value=Telegram()),
                patch("neo_api_client.NeoAPI") as broker,
                redirect_stdout(io.StringIO()),
            ):
                task = asyncio.create_task(
                    serve(
                        cfg,
                        bg.master,
                        paper,
                        None,
                        credentials_override=creds,
                        config_provider=lambda: store.config(uid),
                        account_lock=lambda: store.lock(uid),
                        background=bg,
                    )
                )
                try:
                    await asyncio.wait_for(ready.wait(), 2)
                    await handlers[0][0](event(-100111, 1, "NIFTY 23450 PE BUY 170 TGT 180/200/220 SL 155"))
                    await handlers[0][0](
                        event(-100222, 2, "GOLDM 25 SEP 153000 CALL BUY 170 TGT 180/200/220 SL 155")
                    )
                    broker.assert_not_called()  # No authentication loops or prompts.
                    with sqlite3.connect(paper) as db:
                        self.assertEqual(db.execute("SELECT count(*) FROM source_events").fetchone()[0], 2)
                        state = json.loads(db.execute("SELECT body FROM state").fetchone()[0])
                        self.assertFalse(state["positions"])
                    # A later session cannot replay already archived messages.
                    save(store, uid, creds, dict(edit_token="TOKEN", edit_sid="SID", ucc="UCC"))
                    with sqlite3.connect(paper) as db:
                        self.assertEqual(db.execute("SELECT count(*) FROM source_events").fetchone()[0], 2)
                    self.assertEqual(store.health(uid)["last_channel"], "-100222")
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_ui_auth_while_worker_owns_account_and_tokens_not_returned(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root, Fernet.generate_key())
            uid = store.create_user("alice", "long test password", defaults())
            creds = dict(
                kotak_consumer_key="KEY", kotak_mobile="+919999999999", kotak_ucc="UCC", kotak_mpin="654321"
            )
            store.save_credentials(uid, creds)

            async def check(creds, code):
                return dict(edit_token="SECRET", edit_sid="SID", ucc="UCC")

            connections = Connections(store, broker_check=check)
            with open(store.account_dir(uid) / "worker.lock", "a") as lease:
                fcntl.flock(lease, fcntl.LOCK_EX)
                response = await connections.kotak(uid, "123456")
            self.assertTrue(load(store, uid))
            self.assertNotIn("SECRET", json.dumps(response))
            self.assertNotIn("123456", json.dumps(store.credentials(uid)))

    async def test_feed_failures_do_not_stop_listener_and_new_session_retries(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root, Fernet.generate_key())
            cfg = defaults()
            cfg["live_inputs_enabled"] = True
            cfg["channels"] = {"-100111": {"products": ["NIFTY"], "exit_time_ist": "15:15"}}
            uid = store.create_user("alice", "long test password", cfg)
            creds = dict(telegram_api_id="1234", telegram_api_hash="HASH", kotak_consumer_key="KEY")
            store.save_credentials(uid, creds)
            values = dict(edit_token="SECRET", edit_sid="SID", ucc="UCC")
            save(store, uid, creds, values)
            bg = Background(store, uid, Path(root) / "missing.json")
            handlers, attempts = [], []

            class Telegram:
                async def connect(self):
                    pass

                async def disconnect(self):
                    pass

                def is_connected(self):
                    return True

                async def is_user_authorized(self):
                    return True

                def add_event_handler(self, fn, event):
                    handlers.append(fn)

                async def run_until_disconnected(self):
                    await asyncio.Future()

            class Websocket:
                async def __aenter__(self):
                    raise RuntimeError("SECRET-FEED-ERROR")

                async def __aexit__(self, *args):
                    pass

            class Broker:
                def __init__(self, **kwargs):
                    self.configuration = SimpleNamespace()

                def create_websocket(self, **kwargs):
                    attempts.append(self.configuration.edit_token)
                    return Websocket()

                def totp_login(self, **kwargs):
                    raise AssertionError("Must reuse cached session")

                def totp_validate(self, **kwargs):
                    raise AssertionError("Must reuse cached session")

            real_sleep = asyncio.sleep

            async def fast_sleep(seconds):
                await real_sleep(0.001)

            async def until(predicate):
                for _ in range(200):
                    if predicate():
                        return
                    await real_sleep(0.005)
                self.fail("Worker did not reach expected state")

            output = io.StringIO()
            with (
                patch("telethon.TelegramClient", return_value=Telegram()),
                patch("neo_api_client.NeoAPI", Broker),
                patch("orion.runtime.asyncio.sleep", fast_sleep),
                redirect_stdout(output),
            ):
                task = asyncio.create_task(
                    serve(
                        cfg,
                        bg.master,
                        str(store.account_dir(uid) / "paper.db"),
                        None,
                        credentials_override=creds,
                        config_provider=lambda: store.config(uid),
                        account_lock=lambda: store.lock(uid),
                        background=bg,
                    )
                )
                try:
                    await until(lambda: bg.state["broker"] == "reauthentication_required")
                    self.assertEqual(len(attempts), 3)
                    self.assertFalse(task.done())
                    await handlers[0](
                        SimpleNamespace(
                            chat_id=-100111,
                            id=99,
                            raw_text="Overnight commentary",
                            message=SimpleNamespace(
                                date=datetime.now(timezone.utc), edit_date=None, reply_to_msg_id=None
                            ),
                        )
                    )
                    self.assertEqual(store.health(uid)["last_channel"], "-100111")
                    save(store, uid, creds, {**values, "edit_token": "NEW-TOKEN"})
                    await until(lambda: "NEW-TOKEN" in attempts)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertNotIn("SECRET", output.getvalue())

    async def test_stale_catalogue_keeps_market_status_for_existing_position_quotes(self):
        from unittest.mock import AsyncMock

        with tempfile.TemporaryDirectory() as root:
            store = Store(root, Fernet.generate_key())
            cfg = defaults()
            cfg["live_inputs_enabled"] = True
            cfg["feed_timestamp_unit"] = "seconds"
            cfg["channels"] = {"-100111": {"products": ["NIFTY"], "exit_time_ist": "15:15"}}
            uid = store.create_user("alice", "long test password", cfg)
            creds = dict(telegram_api_id="1234", telegram_api_hash="HASH", kotak_consumer_key="KEY")
            store.save_credentials(uid, creds)
            save(store, uid, creds, dict(edit_token="TOKEN", edit_sid="SID", ucc="UCC"))
            bg = Background(store, uid, Path(root) / "missing.json")
            done = asyncio.Event()
            observed = []

            class Market(SimpleNamespace):
                pass

            class Quote(SimpleNamespace):
                pass

            class Socket:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    pass

                async def subscribe_exchange(self):
                    pass

                def __aiter__(self):
                    return self.messages()

                async def messages(self):
                    yield Market(exchange_segment="nse_fo", status_code=1)
                    for _ in range(2):
                        yield Quote(
                            exchange_segment="nse_fo",
                            instrument_token="123",
                            auction=False,
                            buy=[SimpleNamespace(price=99, quantity=1)],
                            sell=[SimpleNamespace(price=101, quantity=1)],
                            last_update_time=datetime.now(timezone.utc).timestamp(),
                            last_traded_price=100,
                            volume_traded_today=100,
                            open_interest=100,
                        )
                    done.set()
                    await asyncio.Future()

            telegram = SimpleNamespace(
                connect=AsyncMock(),
                disconnect=AsyncMock(),
                is_connected=lambda: True,
                is_user_authorized=AsyncMock(return_value=True),
                add_event_handler=lambda *args: None,
                run_until_disconnected=lambda: asyncio.Future(),
            )
            broker = SimpleNamespace(
                configuration=SimpleNamespace(), create_websocket=lambda **kwargs: Socket()
            )

            def process(engine, event, now):
                observed.append((event, engine.cfg["new_entries_enabled"]))
                return []

            with (
                patch("telethon.TelegramClient", return_value=telegram),
                patch("neo_api_client.NeoAPI", return_value=broker),
                patch("neo_api_client.websocket.feed.SFeedMarketStatus", Market),
                patch("neo_api_client.websocket.feed.SFeedScrip", Quote),
                patch("orion.runtime.active_tokens", return_value={("nse_fo", "123")}),
                patch("orion.runtime.Engine.process", process),
            ):
                task = asyncio.create_task(
                    serve(
                        cfg,
                        bg.master,
                        str(store.account_dir(uid) / "paper.db"),
                        None,
                        credentials_override=creds,
                        config_provider=lambda: store.config(uid),
                        background=bg,
                    )
                )
                try:
                    await asyncio.wait_for(done.wait(), 3)
                    self.assertEqual(len(observed), 2)
                    self.assertTrue(all(event["market_open"] for event, _ in observed))
                    self.assertTrue(all(not enabled for _, enabled in observed))
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
