"""Commit authorized Telegram receipts before fallible downstream work."""
from .core import dumps


def record_and_process(db, event, now, channels, refresh, process):
    if str(event['channel_id']) not in channels:
        return None
    # Separate transaction: a subsequent refresh/process failure must not erase
    # the evidence. This function does not retry or replay stored messages.
    with db:
        db.execute('INSERT OR IGNORE INTO source_events VALUES(?,?,?)',
                   (event['event_id'], now.isoformat(), dumps(event)))
    refresh()
    return process(event, now)


def failure(event, phase, exc):
    # Do not expose exception text, raw messages, credentials or SDK payloads.
    return dict(event='TELEGRAM_HANDLER_FAILED', phase=phase,
                channel_id=str(event.chat_id), message_id=str(event.id),
                error_type=type(exc).__name__)
