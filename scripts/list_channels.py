"""List joined Telegram channels to configure explicit numeric IDs."""

import argparse
from telethon.sync import TelegramClient
from telethon.utils import get_peer_id
from orion.runtime import credentials

p = argparse.ArgumentParser()
p.add_argument("--session", required=True)
a = p.parse_args()
c = credentials()
client = TelegramClient(a.session, int(c["telegram_api_id"]), c["telegram_api_hash"])
client.connect()
try:
    if not client.is_user_authorized():
        raise SystemExit("Run telegram-login first")
    for d in client.iter_dialogs():
        if d.is_channel:
            print(get_peer_id(d.entity), d.name)
finally:
    client.disconnect()
