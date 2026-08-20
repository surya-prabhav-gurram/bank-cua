"""
Resolve an operator ALIAS to the secrets a capability needs.

The problem this closes
-----------------------
Round 1 took credentials as invocation parameters: `--param password=...`. That
put a live banking password into shell history, into `ps` output, and into
anything that logged a command line -- and the redaction model downstream of it,
which is genuinely careful, could do nothing about a leak that happened before
the value ever reached us.

Exposing capabilities over HTTP makes it worse rather than better. If the API
accepts a password in the request body, then every caller holds one, the chatbot
holds one, and the request log of whatever sits in front of the service holds
one. Worse, whoever can call the endpoint picks WHICH operator to be -- so a
teller-level caller could name a supervisor and the system would faithfully sign
on as them. The application's own authorisation boundary would be intact and our
wrapper would have walked around it, which is precisely what the brief warns
against.

The model
---------
Callers name an operator ALIAS ("teller1"). The secret never crosses the wire and
is never returned by any endpoint; it is read at invocation time from a store the
service is configured with, and handed straight to the replay engine, which
already declares it `sensitive` and redacts it everywhere.

The seam: swap the resolver and secrets can come from Vault, AWS Secrets Manager,
or the institution's directory without touching callers, capabilities, or the
engine. `EnvCredentialStore` is the development implementation; the interface is
what production replaces.

Roles live here too, because "may this operator perform a restricted function"
is a property of the identity, not of the request -- and a request that could
assert its own role would not be an authorisation check at all.
"""
from __future__ import annotations

import abc
import json
import os
from dataclasses import dataclass, field


class CredentialError(RuntimeError):
    """An alias could not be resolved.

    Deliberately not a "fall back to no credential" path: a capability invoked
    without the secret it declares would either fail mid-flow on a login screen
    or, worse, proceed as some other identity. Refusing at the boundary keeps the
    failure legible.
    """


@dataclass(frozen=True)
class OperatorIdentity:
    """Who a run is acting as, and what that identity is allowed to be.

    `context` holds NON-secret parameters that belong to the operator rather than
    to the request -- Meridian's branch code, for instance. It is separated from
    `secrets` because the two have opposite disclosure rules: context may be
    shown to a caller, secrets may never be. Folding branch into the request
    instead would make a chatbot responsible for knowing branch codes, and would
    let a caller sign on at a branch it has no business at.
    """
    alias: str
    role: str = "teller"
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    context: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        # The secrets are excluded from the dataclass repr already; this makes the
        # intent explicit so nobody "helpfully" adds them back for debugging.
        return (f"OperatorIdentity(alias={self.alias!r}, role={self.role!r}, "
                f"context={self.context!r}, secrets=<redacted>)")


class CredentialStore(abc.ABC):
    """Where operator secrets come from. The seam production replaces."""

    @abc.abstractmethod
    def resolve(self, alias: str) -> OperatorIdentity: ...

    @abc.abstractmethod
    def aliases(self) -> list[str]:
        """Alias names only -- never secrets. Safe to expose to a caller so a
        chatbot can offer a choice of operator without ever holding one."""


class EnvCredentialStore(CredentialStore):
    """Development store: a JSON file named by an environment variable.

    Format (the file is gitignored; see config/credentials.example.json):

        {"teller1": {"role": "teller",     "secrets": {"password": "..."}},
         "super1":  {"role": "supervisor", "secrets": {"password": "..."}}}

    Read fresh on each resolve rather than cached at import, so rotating a
    secret does not require a restart -- and so a test can point at a fixture
    without the process having already memorised the real one.
    """

    ENV_VAR = "BANKCUA_CREDENTIALS_FILE"
    DEFAULT_PATH = "config/credentials.json"

    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get(self.ENV_VAR, self.DEFAULT_PATH)

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            raise CredentialError(
                f"no credential store at {self.path!r}. Copy "
                f"config/credentials.example.json and set "
                f"{self.ENV_VAR}, or pass a store explicitly.")
        with open(self.path) as fh:
            return json.load(fh)

    def resolve(self, alias: str) -> OperatorIdentity:
        raw = self._load()
        entry = raw.get(alias)
        if entry is None:
            # Names the aliases that DO exist but never the secrets, because the
            # common cause is a typo and the common alternative is an operator
            # guessing in the dark.
            raise CredentialError(
                f"unknown operator alias {alias!r}; configured aliases are "
                f"{sorted(raw)}")
        return OperatorIdentity(alias=alias, role=entry.get("role", "teller"),
                                secrets=dict(entry.get("secrets") or {}),
                                context=dict(entry.get("context") or {}))

    def aliases(self) -> list[str]:
        try:
            return sorted(self._load())
        except CredentialError:
            return []


class StaticCredentialStore(CredentialStore):
    """In-memory store for tests and for the offline demo path."""

    def __init__(self, identities: dict[str, OperatorIdentity]):
        self._identities = identities

    def resolve(self, alias: str) -> OperatorIdentity:
        if alias not in self._identities:
            raise CredentialError(
                f"unknown operator alias {alias!r}; configured aliases are "
                f"{sorted(self._identities)}")
        return self._identities[alias]

    def aliases(self) -> list[str]:
        return sorted(self._identities)
