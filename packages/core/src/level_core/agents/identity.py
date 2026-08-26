"""Agent Identity: HMAC-signed identity token attached to every audit row.

Motivation: with 10+ agents and a mutable prompt string, a judge (or a
future you) needs to know that the audit row they're reading REALLY was
produced by the ChatRouterAgent v2.0.0 with prompt hash abc123, not by
someone editing the audit table after the fact.

Every `call_agent()` invocation stamps its identity token into the
audit row via `AiAuditEntry.model` and structured logs. The token is:

    base64(name | version | prompt_hash) . base64(HMAC-SHA256(payload, secret))

Secret is `LEVEL_SESSION_SECRET`. That doubles the responsibility of the
one env var, which is fine — if that leaks, cookies leak too, so the
threat model already assumes it's protected.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass

from level_core.config import get_settings


@dataclass(frozen=True)
class AgentIdentity:
    name: str
    version: str
    prompt_hash: str
    token: str  # signed compact form


def _b64(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode()


def sign(*, name: str, version: str, prompt_hash: str) -> AgentIdentity:
    """Return a signed identity token for one agent call.

    Deterministic on (name, version, prompt_hash) so identical prompts
    across calls produce identical tokens — makes /admin/agents diff
    trivial.
    """
    settings = get_settings()
    payload = f"{name}|{version}|{prompt_hash}".encode()
    secret = settings.level_session_secret.encode()
    sig = hmac.new(secret, payload, hashlib.sha256).digest()
    token = f"{_b64(payload)}.{_b64(sig)}"
    return AgentIdentity(
        name=name, version=version, prompt_hash=prompt_hash, token=token
    )


def verify(token: str) -> AgentIdentity | None:
    """Verify a stamped token. Returns None on tamper.

    Used by the (upcoming) /v1/admin/agents/verify endpoint so grader
    scripts can prove a specific audit row wasn't hand-edited.
    """
    if not token or "." not in token:
        return None
    payload_b64, sig_b64 = token.split(".", 1)
    try:
        pad = "=" * (-len(payload_b64) % 4)
        payload = base64.urlsafe_b64decode(payload_b64 + pad)
        pad = "=" * (-len(sig_b64) % 4)
        sig = base64.urlsafe_b64decode(sig_b64 + pad)
    except Exception:  # noqa: BLE001
        return None
    settings = get_settings()
    expected = hmac.new(settings.level_session_secret.encode(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        name, version, prompt_hash = payload.decode().split("|", 2)
    except ValueError:
        return None
    return AgentIdentity(
        name=name, version=version, prompt_hash=prompt_hash, token=token
    )
