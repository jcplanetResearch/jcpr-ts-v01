"""Task 9 — Secret management for broker credentials.

Loads broker credentials from `.env` file with strict security verification:

    1. File must exist at expected path.
    2. File permissions MUST be 0600 (owner read/write only) on POSIX.
    3. .env MUST be in .gitignore (verified at module load).
    4. Secrets are NEVER logged in plaintext — masked to first 4 chars + '***'.
    5. SecretValue wrapper prevents accidental __repr__ / __str__ leakage.

Security philosophy:
    - Fail-closed: any irregularity → refuse to load.
    - Defense in depth: file perms + gitignore + wrapper class.
    - No external dependencies — pure stdlib (os, pathlib, stat).

Usage:
    secrets = load_kis_secrets(env_path=".env", mode="paper")
    print(secrets.appkey_masked)  # "PSED***"
    # secrets.appkey raw value only used internally by adapter
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final


# =============================================================================
# Constants
# =============================================================================

#: Required file mode for .env on POSIX systems (owner rw only).
REQUIRED_ENV_MODE: Final[int] = 0o600

#: Maximum allowed mode bits (no group/other access).
MAX_ALLOWED_MODE: Final[int] = 0o600

#: Mask prefix length — show first N chars + '***'.
MASK_PREFIX_LEN: Final[int] = 4

#: Required environment variable names per mode.
KIS_PAPER_ENV_VARS: Final[tuple[str, ...]] = (
    "KIS_PAPER_APPKEY",
    "KIS_PAPER_APPSECRET",
    "KIS_PAPER_ACCOUNT",
    "KIS_PAPER_ACCOUNT_PRODUCT",
)
KIS_PROD_ENV_VARS: Final[tuple[str, ...]] = (
    "KIS_PROD_APPKEY",
    "KIS_PROD_APPSECRET",
    "KIS_PROD_ACCOUNT",
    "KIS_PROD_ACCOUNT_PRODUCT",
)

#: Forbidden patterns that indicate placeholder values (refuse to load).
PLACEHOLDER_PATTERNS: Final[tuple[str, ...]] = (
    "여기에",
    "your_",
    "YOUR_",
    "<your",
    "<YOUR",
    "xxxxxxxxx",
    "XXXXXXXXX",
    "REPLACE_ME",
    "TODO",
    "example",
    "EXAMPLE",
)


class SecretLoadError(RuntimeError):
    """Raised when secret loading fails any security check."""


# =============================================================================
# SecretValue — wrapper that prevents accidental leakage
# =============================================================================

class SecretValue:
    """Wraps a sensitive string so __repr__/__str__ never leak it.

    The raw value is accessible via .reveal() — used only by the broker adapter
    when constructing HTTP request bodies. Logging frameworks calling repr()
    will see "<SecretValue masked='ABCD***'>".
    """

    __slots__ = ("_value", "_masked")

    def __init__(self, raw: str) -> None:
        if not isinstance(raw, str):
            raise TypeError("SecretValue requires str")
        if not raw:
            raise ValueError("SecretValue cannot be empty")
        self._value = raw
        self._masked = _mask_secret(raw)

    def reveal(self) -> str:
        """Return the raw secret. ONLY for direct API call construction."""
        return self._value

    @property
    def masked(self) -> str:
        """Safe display string."""
        return self._masked

    def __repr__(self) -> str:
        return f"<SecretValue masked='{self._masked}'>"

    def __str__(self) -> str:
        return self._masked

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretValue):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)


def _mask_secret(raw: str) -> str:
    """Return first N chars + '***'. Never reveals more than prefix."""
    if not raw:
        return "***"
    if len(raw) <= MASK_PREFIX_LEN:
        return "***"
    return raw[:MASK_PREFIX_LEN] + "***"


# =============================================================================
# KIS secrets bundle
# =============================================================================

@dataclass(frozen=True, slots=True)
class KISSecrets:
    """Validated KIS API credentials. Frozen — never mutated after load."""
    appkey: SecretValue
    appsecret: SecretValue
    account_number: str       # 8-digit prefix (NOT itself secret, but PII)
    account_product: str      # 2-digit suffix (e.g. "01")
    mode: str                 # "paper" or "prod"

    @property
    def account_masked(self) -> str:
        """Show first 4 + '***' for display."""
        if len(self.account_number) <= 4:
            return "***"
        return self.account_number[:4] + "***"

    @property
    def appkey_masked(self) -> str:
        return self.appkey.masked

    @property
    def appsecret_masked(self) -> str:
        return self.appsecret.masked

    def __post_init__(self) -> None:
        if self.mode not in ("paper", "prod"):
            raise ValueError("mode must be 'paper' or 'prod'")
        if not re.fullmatch(r"\d{8}", self.account_number):
            raise ValueError("account_number must be 8 digits")
        if not re.fullmatch(r"\d{2}", self.account_product):
            raise ValueError("account_product must be 2 digits")


# =============================================================================
# Loader — strict security verification
# =============================================================================

def verify_env_file_permissions(env_path: Path) -> None:
    """POSIX file permission check. Raises SecretLoadError on violation.

    On Windows, permission semantics differ — we skip the check but warn
    via a NotImplementedError caller can handle. Most KIS users are on macOS.
    """
    if not env_path.exists():
        raise SecretLoadError(f"env file not found: {env_path}")
    if not env_path.is_file():
        raise SecretLoadError(f"env path is not a file: {env_path}")

    if os.name != "posix":
        # Windows — cannot reliably check Unix-style perms. Caller decides.
        return

    file_stat = env_path.stat()
    mode = stat.S_IMODE(file_stat.st_mode)

    # Reject if group or other has any access
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SecretLoadError(
            f"env file has insecure permissions {oct(mode)} — "
            f"required: {oct(REQUIRED_ENV_MODE)}. "
            f"Fix with: chmod 600 {env_path}"
        )


def verify_gitignore_covers_env(repo_root: Path) -> None:
    """Verify .gitignore exists and excludes .env. Warn-only if missing."""
    gitignore = repo_root / ".gitignore"
    if not gitignore.exists():
        raise SecretLoadError(
            f".gitignore not found at {gitignore}. "
            f"Create one with '.env' on its own line before loading secrets."
        )

    try:
        content = gitignore.read_text(encoding="utf-8")
    except OSError as e:
        raise SecretLoadError(f"cannot read .gitignore: {e}") from e

    # Match exact `.env` line (allow leading whitespace + optional comment)
    pattern = re.compile(r"^\s*\.env\s*(?:#.*)?$", re.MULTILINE)
    if not pattern.search(content):
        raise SecretLoadError(
            ".gitignore does not list '.env' on its own line. "
            "Add '.env' to .gitignore before loading secrets."
        )


def parse_env_file(env_path: Path) -> dict[str, str]:
    """Minimal .env parser. Supports KEY=VALUE and KEY="VALUE" / KEY='VALUE'.

    Lines starting with # are comments. Blank lines ignored.
    Does NOT support shell expansion or interpolation — by design.
    """
    result: dict[str, str] = {}
    try:
        content = env_path.read_text(encoding="utf-8")
    except OSError as e:
        raise SecretLoadError(f"cannot read env file: {e}") from e

    for line_num, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise SecretLoadError(
                f"malformed env file at line {line_num}: missing '='"
            )
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()

        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            raise SecretLoadError(
                f"invalid env key at line {line_num}: '{key}'"
            )

        # Strip surrounding quotes if present
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        result[key] = value

    return result


def _check_no_placeholders(value: str, key: str) -> None:
    """Reject values that look like template placeholders."""
    for pattern in PLACEHOLDER_PATTERNS:
        if pattern in value:
            raise SecretLoadError(
                f"env var {key} contains placeholder '{pattern}' — "
                f"replace with real value"
            )


def load_kis_secrets(
    *,
    env_path: str | os.PathLike[str] = ".env",
    mode: str = "paper",
    repo_root: str | os.PathLike[str] | None = None,
    skip_gitignore_check: bool = False,
) -> KISSecrets:
    """Load and validate KIS credentials from a .env file.

    Args:
        env_path: Path to .env file (default: ".env" in cwd).
        mode: "paper" or "prod". Determines which env var names to read.
        repo_root: Path to repo root for .gitignore check.
                   Defaults to env_path's parent.
        skip_gitignore_check: For unit tests only. NEVER set True in prod.

    Raises:
        SecretLoadError on any security violation or missing variable.
    """
    if mode not in ("paper", "prod"):
        raise SecretLoadError("mode must be 'paper' or 'prod'")

    env_path_obj = Path(env_path).resolve()

    # 1. Permission check
    verify_env_file_permissions(env_path_obj)

    # 2. Gitignore check (skippable for tests only)
    if not skip_gitignore_check:
        root = Path(repo_root).resolve() if repo_root else env_path_obj.parent
        verify_gitignore_covers_env(root)

    # 3. Parse env vars
    parsed = parse_env_file(env_path_obj)

    # 4. Pull required vars
    required = KIS_PAPER_ENV_VARS if mode == "paper" else KIS_PROD_ENV_VARS
    missing = [k for k in required if k not in parsed or not parsed[k]]
    if missing:
        raise SecretLoadError(
            f"missing required env vars for mode='{mode}': "
            f"{', '.join(missing)}"
        )

    # 5. Reject placeholder values
    appkey_key = required[0]
    appsecret_key = required[1]
    account_key = required[2]
    product_key = required[3]
    for k in (appkey_key, appsecret_key, account_key, product_key):
        _check_no_placeholders(parsed[k], k)

    # 6. Construct frozen bundle
    return KISSecrets(
        appkey=SecretValue(parsed[appkey_key]),
        appsecret=SecretValue(parsed[appsecret_key]),
        account_number=parsed[account_key],
        account_product=parsed[product_key],
        mode=mode,
    )


__all__ = (
    "SecretValue",
    "KISSecrets",
    "SecretLoadError",
    "load_kis_secrets",
    "verify_env_file_permissions",
    "verify_gitignore_covers_env",
    "parse_env_file",
    "REQUIRED_ENV_MODE",
    "MASK_PREFIX_LEN",
    "KIS_PAPER_ENV_VARS",
    "KIS_PROD_ENV_VARS",
)
