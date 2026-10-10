"""Account-leased, receive-only Telegram source worker component.

No broker login, BUY transport, service installation or production launcher.
The worker only stages normalized signals. It refuses a busy account before
creating a Telegram client and never requests message history or sends messages.
"""
import asyncio
import fcntl
from datetime import datetime, timezone
import logging

from .ledger import Ledger, Refused
from .routing import SignalRouter, reviewed_policy


def telegram_client(credentials):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    if not all(credentials.get(k) for k in ('telegram_session','telegram_api_id','telegram_api_hash')):
        raise Refused('Saved Telegram authorization required')
    return TelegramClient(StringSession(credentials['telegram_session']),
                          int(credentials['telegram_api_id']), credentials['telegram_api_hash'])


async def serve_signals(store, uid, master_provider, *, stop, _client_factory=telegram_client):
    """Run in an explicitly owned worker slot; no login prompts or auto-restart.

    stop is an asyncio.Event supplied by the eventual lifecycle manager. The
    default factory uses only this account's encrypted, previously saved session.
    Tests inject a synthetic client and never contact Telegram or a broker.
    """
    from telethon import events
    store.user(uid)
    account = store.account_dir(uid)
    client = ledger = None
    tasks = []
    logger = logging.getLogger('telethon')
    prior_level = logger.level
    with open(account/'worker.lock', 'a') as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('Account worker slot busy') from None
        try:
            if not (account/'live.db').is_file():
                raise Refused('Existing live ledger required')
            ledger = Ledger(account/'live.db', uid)
            creds = store.credentials(uid)
            with store.lock(uid):
                reviewed_policy(store, uid, creds.get('kotak_ucc'))
            router = SignalRouter(store, uid, ledger, creds['kotak_ucc'])
            ledger.db.execute('''CREATE TABLE IF NOT EXISTS live_source_health(
                id INTEGER PRIMARY KEY CHECK(id=1), checked_at TEXT NOT NULL,
                state TEXT NOT NULL, accepted INTEGER NOT NULL, refused INTEGER NOT NULL)''')
            ledger.db.execute("INSERT OR IGNORE INTO live_source_health VALUES(1,?,'starting',0,0)", (datetime.now(timezone.utc).isoformat(),))
            # Disconnected/restarted collection cannot revive an old candidate.
            ledger.db.execute("UPDATE live_signals SET state='INVALIDATED' WHERE state='PENDING'")
            def health(state, accepted=0, refused=0):
                ledger.db.execute('UPDATE live_source_health SET checked_at=?,state=?,accepted=accepted+?,refused=refused+? WHERE id=1',
                                  (datetime.now(timezone.utc).isoformat(),state,accepted,refused))
            boot = datetime.now(timezone.utc)
            def selected(event):
                # Check ownership/allowlist before accessing any channel text.
                return str(event.chat_id) in store.user(uid)['settings'].get('channels', {})
            async def message(event):
                if not selected(event):
                    return
                if event.message.date < boot and not event.message.edit_date:
                    return
                try:
                    result = router.message(channel=str(event.chat_id), message_id=event.id,
                        text=event.raw_text or '', source_at=event.message.date,
                        received_at=datetime.now(timezone.utc), master=master_provider(),
                        edited=event.message.edit_date is not None, reply_to=event.message.reply_to_msg_id)
                    health('connected', accepted=int(not result['duplicate']))
                except Exception:
                    # Deliberately no source text, credentials or exception text.
                    health('signal-refused', refused=1)
            logger.setLevel(logging.CRITICAL)
            client = _client_factory(creds)
            del creds
            client.add_event_handler(message, events.NewMessage(func=selected))
            client.add_event_handler(message, events.MessageEdited(func=selected))
            await asyncio.wait_for(client.connect(), timeout=10)
            if not await asyncio.wait_for(client.is_user_authorized(), timeout=5):
                raise Refused('Saved Telegram authorization expired')
            health('connected')
            tasks = [asyncio.ensure_future(client.run_until_disconnected()), asyncio.create_task(stop.wait())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if tasks[0] in done and not stop.is_set():
                raise Refused('Telegram source disconnected')
            health('stopped')
        except asyncio.CancelledError:
            raise
        except Exception:
            if ledger is not None and ledger.db.execute("SELECT 1 FROM sqlite_master WHERE name='live_source_health'").fetchone():
                ledger.db.execute("UPDATE live_source_health SET state='disconnected',checked_at=? WHERE id=1", (datetime.now(timezone.utc).isoformat(),))
            raise Refused('Live signal source stopped; inspect account health') from None
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                if client is not None:
                    try:
                        await asyncio.wait_for(client.disconnect(), timeout=10)
                    except Exception:
                        pass
            finally:
                logger.setLevel(prior_level)
                if ledger is not None:
                    if ledger.db.execute("SELECT 1 FROM sqlite_master WHERE name='live_signals'").fetchone():
                        ledger.db.execute("UPDATE live_signals SET state='INVALIDATED' WHERE state='PENDING'")
                    ledger.close()
