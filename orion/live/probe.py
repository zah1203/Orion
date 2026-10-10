"""One-shot child process: credentials via stdin, sanitized summary via stdout."""
import asyncio
import contextlib
import json
import logging
import os
import sys
import tempfile
from .readonly import CODES, ProbeFailure, number, probe


async def run_probe(creds, totp):
    # Only a minimal environment crosses the process boundary. SDK caches, if any,
    # are directed to a private temporary directory and removed after the process.
    with tempfile.TemporaryDirectory(prefix='orion-readonly-') as temp:
        env = {'PATH': os.defpath, 'NEO_LOG_FILE_ENABLED': 'false',
               'NEO_HOLDINGS_CACHE_DIR': temp, 'TMPDIR': temp}
        child = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'orion.live.probe', env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            try:
                data, _ = await asyncio.wait_for(child.communicate(
                    json.dumps({'creds': creds, 'totp': totp}).encode()), timeout=60)
            except asyncio.TimeoutError:
                raise ProbeFailure('timeout') from None
            if len(data) > 4096:
                raise ProbeFailure('probe-failed')
            report = json.loads(data)
            if child.returncode:
                code = report.get('error') if isinstance(report, dict) else None
                raise ProbeFailure(code if code in CODES else 'probe-failed')
            expected = {'order_count','working_order_count','position_count','nonzero_position_count',
                        'rms_net','identity_matched','live_available','order_submission_available','checked_at'}
            if not isinstance(report, dict) or set(report) != expected:
                raise ProbeFailure('probe-failed')
            for k in ('order_count','working_order_count','position_count','nonzero_position_count'):
                if type(report[k]) is not int or not 0 <= report[k] <= 10000:
                    raise ProbeFailure('probe-failed')
            if (report['identity_matched'] is not True or report['live_available'] is not False or
                    report['order_submission_available'] is not False):
                raise ProbeFailure('probe-failed')
            report['rms_net'] = str(number(report['rms_net']))
            from datetime import datetime, timezone
            stamp = datetime.fromisoformat(report['checked_at'])
            if stamp.tzinfo is None or not 0 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 65:
                raise ProbeFailure('probe-failed')
            report['checked_at'] = stamp.isoformat()
            return report
        except ProbeFailure:
            raise
        except Exception:
            raise ProbeFailure('probe-failed') from None
        finally:
            if child.returncode is None:
                child.kill()
            await child.wait()


def main():
    os.umask(0o077)
    os.environ['NEO_LOG_FILE_ENABLED'] = 'false'
    logging.disable(logging.CRITICAL)
    result, code = {'error': 'probe-failed'}, 1
    # Suppress all SDK output, including errors containing tokens or account data.
    with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
        try:
            payload = json.loads(sys.stdin.buffer.read(32769))
            creds = payload['creds']
            from neo_api_client import NeoAPI
            client = NeoAPI(consumer_key=creds['kotak_consumer_key'], environment='prod')
            result = probe(client, creds, payload['totp'])
            code = 0
        except ProbeFailure as exc:
            result = {'error': exc.code}
        except Exception:
            pass
    print(json.dumps(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
