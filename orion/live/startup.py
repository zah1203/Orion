"""Explicit reviewed-session startup for existing-exposure management.

No service registration, automatic login, entry authorization or TOTP storage.
The caller must own the account-specific startup/hand-over decision.
"""
import re

from .ledger import Refused, positive_int
from .monitor import AccountMonitor
from .routing import reviewed_policy
from .session import ProcessSession


class ProtectiveSession:
    """Bounded session capability that cannot place an ENTRY command."""
    def __init__(self, session):
        self._session = session
        self.ucc = session.ucc

    def request(self, operation, **kwargs):
        if self._session.ucc != self.ucc:
            raise Refused('Protective session account changed')
        if operation == 'place':
            request = kwargs.get('request')
            if not isinstance(request, dict) or request.get('kind') not in ('STOP', 'EXIT'):
                raise Refused('Reviewed startup does not authorize BUY orders')
        elif operation not in ('snapshot', 'evidence', 'quotes', 'margin', 'funding', 'cancel'):
            raise Refused('Unsupported protective session operation')
        return self._session.request(operation, **kwargs)

    def close(self):
        self._session.close()


class ReviewedAccountMonitor(AccountMonitor):
    """Authenticate once, only after the shared worker lease and ledger checks.

    This is a component for an explicit future launcher, not a service or a Live
    activation switch. Revocation prevents new startup. Once started, the existing
    monitor can continue protecting exposure without granting new entry authority.
    """
    def __init__(self, store, uid, policy_version, totp, *, strategy=False,
                 collect_trades=True, _session_factory=ProcessSession):
        positive_int(policy_version)
        if not isinstance(totp, str) or not re.fullmatch(r'[0-9]{6}', totp):
            raise Refused('One current authentication code required')
        self._totp = totp
        self._policy_version = policy_version
        self._session_factory = _session_factory
        super().__init__(store, uid, self._authenticate, strategy=strategy,
                         collect_trades=collect_trades)

    def __enter__(self):
        try:
            return super().__enter__()
        except BaseException:
            self._totp = None
            raise Refused('Reviewed account startup failed') from None

    def _authenticate(self):
        if self.ledger is None or self.lease is None or self._totp is None:
            raise Refused('One-shot leased authentication required')
        code, self._totp = self._totp, None
        session = None
        with self.store.lock(self.uid):
            try:
                credentials = self.store.credentials(self.uid)
                row = self.ledger.db.execute('SELECT ucc FROM reconciliation_account').fetchone()
                if not row or row[0] != credentials.get('kotak_ucc'):
                    raise Refused('Saved credentials must match the existing ledger')
                ucc = row[0]
                policy = reviewed_policy(self.store, self.uid, ucc)
                if policy['version'] != self._policy_version:
                    raise Refused('Startup policy version changed')
                session = self._session_factory(credentials, code)
                # Do not pass a newly authenticated session to the worker if
                # approval or credentials changed during the bounded login.
                current = reviewed_policy(self.store, self.uid, ucc)
                if (session.ucc != ucc or current != policy or
                        self.store.credentials(self.uid) != credentials):
                    raise Refused('Startup account review changed during authentication')
                return ProtectiveSession(session)
            except BaseException:
                if session is not None:
                    session.close()
                raise
            finally:
                code = None

    def __exit__(self, *unused):
        # Clear an unconsumed code too (busy Paper lease, missing ledger, etc.).
        # Python cannot guarantee zeroing string bytes; nothing is persisted.
        self._totp = None
        return super().__exit__(*unused)
