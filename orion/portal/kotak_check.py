"""Short-lived SDK authentication probe. Credentials arrive via stdin, never argv."""

import contextlib
import json
import logging
import os
import sys


def success(response):
    if not isinstance(response, dict) or response.get("error"):
        return False
    data = response.get("data")
    return (
        isinstance(data, dict)
        and data.get("status") == "success"
        and bool(data.get("token"))
        and bool(data.get("sid"))
    )


def main():
    payload = json.load(sys.stdin)
    ok = False
    session = None
    os.environ["NEO_LOG_FILE_ENABLED"] = "false"
    logging.disable(logging.CRITICAL)
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            from neo_api_client import NeoAPI

            creds = payload["creds"]
            client = NeoAPI(consumer_key=creds["kotak_consumer_key"], environment="prod")
            first = client.totp_login(
                mobile_number=creds["kotak_mobile"], ucc=creds["kotak_ucc"], totp=payload["totp"]
            )
            if success(first):
                ok = success(client.totp_validate(mpin=creds["kotak_mpin"]))
            if ok:
                from .broker_session import capture

                session = capture(client)
            # Session travels only over the private parent/child pipe, never logs.
        except Exception:
            pass
    print(json.dumps(session) if ok and session else "FAILED")
    return 0 if ok and session else 1


if __name__ == "__main__":
    sys.exit(main())
