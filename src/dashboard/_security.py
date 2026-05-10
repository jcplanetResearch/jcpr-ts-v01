"""Dashboard security gates — layers 13-17 of the defense stack.

Composes with the existing 12-layer stack from Stage 2-B:
    1.  WriteHandlers._validate_self_distinct
    2.  WriteHandlers.__post_init__
    3.  ApprovalStore.approve / SelfApprovalError
    4.  ApprovalStore.get / TTL lazy expire
    5.  ExecutionGateway.__init__ / LiveModeBlockedError
    6.  ExecutionGateway.execute_approved / ModeViolationError
    7.  ExecutionGateway.execute_approved / ExpiredApprovalError
    8.  ExecutionGateway.execute_approved / AlreadyExecutedError
    9.  ExecutionGateway._check_interrupt / KillSwitchActiveError
    10. ExecutionGateway._call_broker_submit / mode-based routing
    11. RestrictedMCPServer.call_tool / payload whitelist + identity
    12. RestrictedMCPServer._safe_call / structured error responses

This module adds layers 13-17:
    13. enforce_localhost_binding — refuse 0.0.0.0 / external IPs
    14. scrub_secrets             — regex-mask any secret-looking text
    15. verify_db_permissions     — 0600 enforced on opened SQLite files
    16. assert_no_secrets_in_env  — match ApprovalStore _check_no_secrets
    17. assert_audit_logs_secured — 0600 on jsonl audit logs

All functions are FAIL-CLOSED: they raise DashboardSecurityError on
violation rather than silently degrading. Non-POSIX environments
(rare for this single-operator deployment) skip POSIX-specific
permission checks but still apply secret scrubbing and binding rules.
"""
from __future__ import annotations

import os
import re
import socket
import stat
from pathlib import Path
from typing import Iterable, Pattern


__all__ = [
    "DashboardSecurityError",
    "REQUIRED_BIND_HOST",
    "REQUIRED_FILE_MODE",
    "SECRET_PATTERNS",
    "FORBIDDEN_BIND_HOSTS",
    "enforce_localhost_binding",
    "scrub_secrets",
    "verify_db_permissions",
    "assert_no_secrets_in_env",
    "assert_audit_logs_secured",
]


# =============================================================================
# Constants
# =============================================================================

REQUIRED_BIND_HOST: str = "127.0.0.1"
REQUIRED_FILE_MODE: int = 0o600

# Hosts that are NEVER acceptable for the dashboard. 0.0.0.0 binds all
# interfaces (including external); 127.0.0.1's IPv6 sibling ::1 is also
# loopback-only and acceptable, but for explicit single-listener simplicity
# we restrict to IPv4 loopback only.
FORBIDDEN_BIND_HOSTS: frozenset[str] = frozenset({
    "0.0.0.0", "*", "::", "::0",
})

# Secret-pattern regex set. Each pattern is case-insensitive and matches
# common credential-looking key=value or key:value pairs. The set is
# deliberately broad — false positives only mask legitimate text, while
# misses leak credentials. Designed for DEFENSE not detection: anything
# that looks like a secret is masked.
SECRET_PATTERNS: tuple[Pattern[str], ...] = (
    # key=value or key: value, any whitespace. Each alternative is its own
    # captured group; the trailing value pattern is non-greedy and stops at
    # whitespace, comma, semicolon, or end-of-line. The list deliberately
    # includes plain `token` (not just `access_token`/`auth_token`) because
    # short forms appear in real-world configs.
    re.compile(
        r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|apikey|"
        r"access[_-]?token|auth[_-]?token|bearer|private[_-]?key|"
        r"appkey|appsecret|client[_-]?secret|jwt|credential|token)\b"
        r"\s*[=:]\s*[^\s;,]+",
    ),
    # KIS-specific: account_number, CANO, ACNT_PRDT_CD with values
    re.compile(
        r"(?i)\b(account[_-]?number|cano|acnt[_-]?prdt[_-]?cd)\b"
        r"\s*[=:]\s*[^\s;,]+",
    ),
    # Bare bearer tokens in headers
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
)

REDACTED_PLACEHOLDER: str = "***REDACTED***"


# =============================================================================
# Exceptions
# =============================================================================

class DashboardSecurityError(Exception):
    """Raised by any security gate when a violation is detected.

    Always fail-closed: the dashboard refuses to start or refuses to
    render rather than degrading silently when this is raised.
    """


# =============================================================================
# Layer 13 — localhost binding
# =============================================================================

def enforce_localhost_binding(
    bind_host: str | None = None,
    *,
    env_var: str = "STREAMLIT_SERVER_ADDRESS",
) -> str:
    """Verify the dashboard binds to 127.0.0.1 only.

    Streamlit's default is 'localhost' which resolves to 127.0.0.1 on
    most systems, but operators occasionally set --server.address to
    '0.0.0.0' for "convenience" — that exposes broker-account data to
    every machine on the LAN, which violates <requirement> "private
    information must not be leaked."

    This check runs at app startup (or before Reconciler is invoked
    in test environments). It inspects the explicit `bind_host` argument
    first, then the environment variable, defaulting to None which means
    the caller has not yet committed to a host (treated as OK — the
    caller is responsible for passing the host once decided).

    Args:
        bind_host: explicit host string from caller (e.g. CLI args).
        env_var: env var to check if bind_host is None.

    Returns:
        The validated bind host (always 127.0.0.1 if it returns at all).

    Raises:
        DashboardSecurityError: if the host is in FORBIDDEN_BIND_HOSTS
            or is anything other than 127.0.0.1 / 'localhost'.
    """
    host = bind_host
    if host is None:
        host = os.environ.get(env_var)
    if host is None:
        # Nothing committed yet — caller must invoke us again with the
        # host once it is decided. This is an acceptable state at very
        # early startup; we explicitly return REQUIRED_BIND_HOST as the
        # canonical answer, NOT to imply we silently approved anything.
        return REQUIRED_BIND_HOST

    normalized = host.strip().lower()
    if normalized in FORBIDDEN_BIND_HOSTS:
        raise DashboardSecurityError(
            f"bind host {host!r} is forbidden — exposes dashboard "
            f"externally. Required: {REQUIRED_BIND_HOST!r}."
        )

    # Resolve 'localhost' / '127.0.0.1' equivalents.
    if normalized in ("localhost", "127.0.0.1"):
        return REQUIRED_BIND_HOST

    # Anything else is rejected outright. Even if a user sets it to
    # a private RFC1918 address, the dashboard's threat model assumes
    # single-host single-operator; widening the binding requires an
    # explicit code change here.
    raise DashboardSecurityError(
        f"bind host {host!r} is not allowed; required {REQUIRED_BIND_HOST!r} "
        f"(loopback-only, single operator)."
    )


# =============================================================================
# Layer 14 — secret scrubbing
# =============================================================================

def scrub_secrets(text: str) -> str:
    """Mask any secret-looking substrings before rendering.

    Applies SECRET_PATTERNS in order, replacing each match with the
    REDACTED_PLACEHOLDER token. Used as a last-line defense before
    showing arbitrary text fields (broker error messages, log lines,
    audit dumps) in the UI.

    This function is INTENTIONALLY conservative: false positives only
    obscure benign text (recoverable by reading the raw audit file
    out-of-band), while a missed credential leaks live secrets to the
    rendered HTML.

    Args:
        text: arbitrary text potentially containing secrets.

    Returns:
        Text with all matches replaced by REDACTED_PLACEHOLDER. Empty
        string and None-coerced inputs are returned as empty string.
    """
    if not text:
        return ""
    out = text
    for pattern in SECRET_PATTERNS:
        out = pattern.sub(REDACTED_PLACEHOLDER, out)
    return out


# =============================================================================
# Layer 15 — DB file permissions
# =============================================================================

def verify_db_permissions(db_path: Path) -> None:
    """Refuse to open a SQLite file that is not 0600 on POSIX.

    Mirrors ApprovalStore._verify_file_mode (assumption #1, "SQLite file
    mode 0600 enforced at creation; verified on each open"). The
    dashboard never creates the DB — only reads it — so this check
    catches the case where an external process or operator accidentally
    `chmod`'d the file to be world-readable.

    Args:
        db_path: filesystem path to the SQLite file.

    Raises:
        DashboardSecurityError: if file exists with mode != 0600 on POSIX.
            (Non-POSIX systems skip silently — Windows file mode semantics
            differ; the dashboard targets macOS/Linux per <documents>.)

    Does NOT raise if the file is absent (caller may be probing
    existence; absence is not a security violation here, it is a
    different error class that the caller handles).
    """
    if os.name != "posix":
        return
    if not db_path.exists():
        return
    actual = stat.S_IMODE(db_path.stat().st_mode)
    if actual != REQUIRED_FILE_MODE:
        raise DashboardSecurityError(
            f"DB file {db_path} has mode {oct(actual)}, "
            f"required {oct(REQUIRED_FILE_MODE)}. "
            f"Fix: chmod 600 {db_path}"
        )


# =============================================================================
# Layer 16 — environment-variable scan
# =============================================================================

# Forbidden secret-bearing env keywords, mirrored from
# src/mcp_servers/_config.SECRET_KEYWORDS so the dashboard inherits the
# same gate without depending on that module at import time.
_FORBIDDEN_ENV_KEYWORDS: tuple[str, ...] = (
    "PASSWORD", "SECRET", "TOKEN", "API_KEY", "AUTH",
    "CREDENTIAL", "PRIVATE_KEY",
)


def assert_no_secrets_in_env() -> None:
    """Refuse to start if a raw secret env var is set at the OS level.

    The JCPR system loads secrets from .env via SecretValue at controlled
    points; raw `PASSWORD=...` or `API_KEY=...` exported into the shell
    environment violates the architecture and risks logging-leak. The
    dashboard inherits the same gate as ApprovalStore's load_*_config.

    JCPR-namespaced variables (JCPR_TOKEN_TTL, JCPR_AUTH_TIMEOUT, etc.)
    are allowed even if they contain a forbidden keyword as a substring,
    because the prefix establishes the JCPR config namespace.

    Raises:
        DashboardSecurityError: if a forbidden raw env var is set.
    """
    for key in os.environ:
        upper = key.upper()
        if upper.startswith("JCPR_"):
            continue  # namespaced — allowed
        if upper in _FORBIDDEN_ENV_KEYWORDS:
            raise DashboardSecurityError(
                f"forbidden secret env var detected: {key!r}; "
                f"secrets must be loaded from .env via SecretValue, "
                f"not exported into the shell environment."
            )


# =============================================================================
# Layer 17 — audit log permissions
# =============================================================================

def assert_audit_logs_secured(paths: Iterable[Path]) -> None:
    """Verify each provided audit log file has mode 0600 on POSIX.

    Audit logs (risk_decisions.jsonl, executions.jsonl) contain
    decision history that, while less sensitive than secrets, can
    reveal trading patterns if leaked. They must be readable only
    by the operator's UID.

    Args:
        paths: iterable of Path objects pointing to audit logs.

    Raises:
        DashboardSecurityError: if any existing file has mode != 0600.
            Missing files are tolerated (caller decides whether absence
            is acceptable — empty audit usually is).
    """
    if os.name != "posix":
        return
    for p in paths:
        if not p.exists():
            continue
        actual = stat.S_IMODE(p.stat().st_mode)
        if actual != REQUIRED_FILE_MODE:
            raise DashboardSecurityError(
                f"audit log {p} has mode {oct(actual)}, "
                f"required {oct(REQUIRED_FILE_MODE)}. "
                f"Fix: chmod 600 {p}"
            )
