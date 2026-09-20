import argparse
import asyncio
import json
import os
from pathlib import Path
from .core import Engine, dumps, stamp


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description="Orion India — simulation only")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("replay", "serve"):
        q = sub.add_parser(name)
        q.add_argument("--config", required=True)
        q.add_argument("--master", required=True)
        q.add_argument("--db", required=True)
        if name == "replay":
            q.add_argument("--events", required=True)
        else:
            q.add_argument("--session", required=True)
    q = sub.add_parser("telegram-login")
    q.add_argument("--session", required=True)
    args = p.parse_args()
    if args.command == "telegram-login":
        from .runtime import telegram_login

        telegram_login(args.session)
        return
    config = json.loads(Path(args.config).read_text())
    master = json.loads(Path(args.master).read_text())
    if args.command == "serve":
        from .runtime import serve

        asyncio.run(serve(config, master, args.db, args.session))
        return
    engine = Engine(args.db, config, master, allow_synthetic=True)
    with open(args.events) as source:
        for line in source:
            if line.strip():
                event = json.loads(line)
                for result in engine.process(event, stamp(event.get("received_at", event["source_time"]))):
                    print(dumps(result))
    print(dumps({"paper_state": engine.state()}))


if __name__ == "__main__":
    main()
