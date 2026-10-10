"""Bounded, account-bound process session. Not wired to production entrypoints.

Credentials and responses use an anonymous pipe, never argv/environment/files.
Timeout/cancellation kills and reaps the child; callers must retain UNKNOWN
dispatch state. A new session is never an instruction to resend an old intent.
"""
from decimal import Decimal
import json
import logging
import multiprocessing
import os
import resource
import tempfile
import threading

from .kotak import KotakSession, OrderRequest, TransportFailure, make_client
from .reconciliation import text_field

MAX_FRAME = 2 * 1024 * 1024


def _encode(value):
    def scalar(v):
        if isinstance(v, Decimal):
            return str(v)
        raise TypeError
    data = json.dumps(value, default=scalar, allow_nan=False).encode()
    if len(data) > MAX_FRAME:
        raise TransportFailure('Broker frame too large')
    return data


def _child(pipe, factory, temp):
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Suppress native and Python SDK output. Pipe traffic is unaffected.
    with open(os.devnull, 'w') as quiet:
        os.dup2(quiet.fileno(), 1)
        os.dup2(quiet.fileno(), 2)
    logging.disable(logging.CRITICAL)
    os.environ.clear()
    os.environ.update(PATH=os.defpath, NEO_LOG_FILE_ENABLED='false',
                      NEO_HOLDINGS_CACHE_DIR=temp, TMPDIR=temp)
    os.chdir(temp)
    try:
        payload = json.loads(pipe.recv_bytes(MAX_FRAME))
        client = factory(payload['creds'])
        session = KotakSession(client, payload['creds'], payload['totp'])
        del payload
        pipe.send_bytes(_encode({'ok': True}))
        while True:
            command = json.loads(pipe.recv_bytes(MAX_FRAME))
            operation = command['operation']
            if operation == 'place':
                result = {'broker_id': session.place(OrderRequest(**command['request']))}
            elif operation == 'cancel':
                result = {'broker_id': session.cancel(command['broker_id'])}
            elif operation == 'snapshot':
                result = session.snapshot()
            else:
                raise ValueError
            pipe.send_bytes(_encode({'ok': True, 'result': result}))
    except EOFError:
        pass
    except BaseException:
        # Even an SDK error containing credentials cannot enter the parent.
        try:
            pipe.send_bytes(_encode({'ok': False}))
        except (OSError, EOFError):
            pass
    finally:
        pipe.close()


class ProcessSession:
    def __init__(self, creds, totp, *, timeout=25, _factory=make_client):
        if not 0 < timeout <= 60:
            raise ValueError('Bounded broker timeout required')
        self.timeout = timeout
        self.ucc = text_field(creds.get('kotak_ucc'))
        self.lock = threading.Lock()
        self.closed = False
        ctx = multiprocessing.get_context('spawn')
        self.pipe, remote = ctx.Pipe()
        self.temp = tempfile.TemporaryDirectory(prefix='orion-live-session-')
        self.child = ctx.Process(target=_child, args=(remote, _factory, self.temp.name), daemon=True)
        try:
            self.child.start()
            remote.close()
            self._exchange({'creds': creds, 'totp': totp})
        except BaseException:
            remote.close()
            self.close()
            raise

    def _exchange(self, command):
        if self.closed:
            raise TransportFailure('Broker session closed')
        # Never wait indefinitely to write a large message to a stalled process.
        # Requests are tiny; large book frames travel only child -> parent.
        data = _encode(command)
        if len(data) > 16384:
            raise TransportFailure('Broker command too large')
        outcome = []
        def exchange():
            try:
                self.pipe.send_bytes(data)
                outcome.append(self.pipe.recv_bytes(MAX_FRAME))
            except BaseException:
                outcome.append(None)
        io = threading.Thread(target=exchange, daemon=True)
        try:
            io.start()
            io.join(self.timeout)
            if io.is_alive() or not outcome or outcome[0] is None:
                raise TimeoutError
            response = json.loads(outcome[0])
            if not isinstance(response, dict) or response.get('ok') is not True:
                raise ValueError
            return response.get('result')
        except BaseException as exc:
            self.close()
            io.join(timeout=1)
            if not isinstance(exc, Exception):
                raise
            raise TransportFailure('Broker session failed; reconcile pending commands') from None

    def request(self, operation, **arguments):
        if operation not in ('place', 'cancel', 'snapshot'):
            raise ValueError('Unsupported broker operation')
        with self.lock:
            return self._exchange(dict(operation=operation, **arguments))

    def close(self):
        if not self.closed:
            self.closed = True
            if self.child.is_alive():
                self.child.kill()
            if self.child.pid is not None:
                self.child.join(timeout=5)
            self.pipe.close()
            if self.child.is_alive():
                raise TransportFailure('Broker process could not be reaped')
            self.temp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()
