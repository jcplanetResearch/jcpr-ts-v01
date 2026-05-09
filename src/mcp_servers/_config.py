"""MCP server configuration — Phase 2 (unified approval store path).

Both readonly and restricted MCP servers read configuration from
environment variables here. Phase 2 change: `JCPR_APPROVAL_DB` is now the
single source of truth for the approval store path. The legacy variables
`JCPR_MCP_APPROVAL_DB` and `JCPR_EXEC_APPROVAL_DB` are removed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


__all__ = [
    "ConfigError",
    "ReadOnlyServerConfig",
    "RestrictedServerConfig",
    "ServerConfig",  # alias of RestrictedServerConfig
    "load_readonly_config",
    "load_restricted_config",
]


logger = logging.getLogger(__name__)


# Forbidden secret-bearing env keywords (defense in depth — secrets must
# come via .env / SecretValue, never via top-level config env vars)
SECRET_KEYWORDS = (
    "PASSWORD", "SECRET", "TOKEN", "API_KEY", "AUTH",
    "CREDENTIAL", "PRIVATE_KEY",
)


class ConfigError(Exception):
    """Raised when MCP server configuration fails validation."""


# ---------------------------------------------------------------------------
# Read-only server config (Task 34) — unchanged from prior version
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ReadOnlyServerConfig:
    """Configuration for the read-only MCP server."""

    positions_db: Path
    audit_dir: Path
    ohlcv_db: Optional[Path] = None
    quote_db: Optional[Path] = None
    risk_audit_dir: Optional[Path] = None
    exec_audit_dir: Optional[Path] = None
    strategy_registry_path: Optional[Path] = None
    rate_limit_per_minute: int = 120
    rate_limit_per_hour: int = 2000
    bind_host: str = "127.0.0.1"

    def __post_init__(self) -> None:
        if not self.positions_db:
            raise ConfigError("positions_db is required")
        if not self.audit_dir:
            raise ConfigError("audit_dir is required")


def load_readonly_config() -> ReadOnlyServerConfig:
    """Load read-only server config from environment."""
    _check_no_secrets_in_env()

    positions_db = os.getenv("JCPR_POSITIONS_DB")
    audit_dir = os.getenv("JCPR_AUDIT_DIR")
    if not positions_db:
        raise ConfigError("JCPR_POSITIONS_DB env var required")
    if not audit_dir:
        raise ConfigError("JCPR_AUDIT_DIR env var required")

    return ReadOnlyServerConfig(
        positions_db=Path(positions_db).resolve(),
        audit_dir=Path(audit_dir).resolve(),
        ohlcv_db=_optional_path("JCPR_OHLCV_DB"),
        quote_db=_optional_path("JCPR_QUOTE_DB"),
        risk_audit_dir=_optional_path("JCPR_RISK_AUDIT"),
        exec_audit_dir=_optional_path("JCPR_EXEC_AUDIT"),
        strategy_registry_path=_optional_path("JCPR_STRATEGY_REGISTRY"),
        rate_limit_per_minute=_int_env("JCPR_MCP_RATE_LIMIT_PM", 120),
        rate_limit_per_hour=_int_env("JCPR_MCP_RATE_LIMIT_PH", 2000),
        bind_host="127.0.0.1",
    )


# ---------------------------------------------------------------------------
# Restricted server config (Task 35 + Phase 2 unification)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RestrictedServerConfig:
    """Configuration for the restricted (write) MCP server.

    Phase 2 change: `approval_db_path` is read from JCPR_APPROVAL_DB only.
    The legacy JCPR_MCP_APPROVAL_DB and JCPR_EXEC_APPROVAL_DB env vars are
    no longer recognised and will be rejected if present (migration aid).

    Phase 2-B addition: `project_root` is accepted as an alternative anchor
    for audit-log placement. When provided and `audit_dir` is not given,
    `audit_dir` is auto-derived as `project_root / "audit"`. At least one
    of `audit_dir` or `project_root` MUST be supplied — this preserves the
    fail-closed audit-path guarantee (no silent omission of the audit
    trail). The production env loader `load_restricted_config()` continues
    to require `JCPR_AUDIT_DIR` explicitly; `project_root` is a
    convenience for integration-test fixtures that anchor the entire
    sandbox under `tmp_path`.
    """

    approval_db_path: Path
    audit_dir: Optional[Path] = None
    project_root: Optional[Path] = None
    mode: str = "paper"        # "paper" or "live"
    allow_live: bool = False
    operator_id: str = "operator-jcpr"
    rate_limit_per_minute: int = 30
    rate_limit_per_hour: int = 300
    bind_host: str = "127.0.0.1"
    proposal_ttl_seconds: int = 300
    execution_ttl_seconds: int = 60
    kill_switch_ttl_seconds: int = 60
    db_file_mode: int = 0o600

    def __post_init__(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ConfigError(f"mode must be 'paper' or 'live', got {self.mode!r}")
        if self.mode == "live" and not self.allow_live:
            raise ConfigError(
                "live mode requires allow_live=True (set JCPR_ALLOW_LIVE=1)"
            )
        if not self.approval_db_path or str(self.approval_db_path) in ("", "."):
            raise ConfigError("approval_db_path is required (non-empty)")

        # audit_dir / project_root — at least one is mandatory.
        # Fail-closed: an audit-log path cannot be silently omitted.
        if not self.audit_dir and not self.project_root:
            raise ConfigError(
                "either audit_dir or project_root must be provided "
                "(audit log path cannot be omitted)"
            )

        # If only project_root is given, derive audit_dir = project_root/audit.
        # frozen dataclass: use object.__setattr__ (Python standard pattern
        # for in-__post_init__ normalization of frozen instances).
        if self.audit_dir is None and self.project_root is not None:
            derived = Path(self.project_root).resolve() / "audit"
            object.__setattr__(self, "audit_dir", derived)

        # Normalize project_root to absolute path for consistency, if given.
        if self.project_root is not None:
            object.__setattr__(
                self, "project_root", Path(self.project_root).resolve()
            )

        if not self.operator_id:
            raise ConfigError("operator_id is required")


def load_restricted_config() -> RestrictedServerConfig:
    """Load restricted server config from environment.

    Required env:
      - JCPR_APPROVAL_DB        path to unified approvals.sqlite
      - JCPR_AUDIT_DIR          path to audit log directory

    Optional:
      - JCPR_MODE               'paper' (default) or 'live'
      - JCPR_ALLOW_LIVE         '1' to permit live mode
      - JCPR_OPERATOR_ID        defaults to 'operator-jcpr'
    """
    _check_no_secrets_in_env()
    _reject_legacy_approval_vars()

    approval_db = os.getenv("JCPR_APPROVAL_DB")
    if not approval_db:
        raise ConfigError(
            "JCPR_APPROVAL_DB env var required (Phase 2: single unified path)"
        )
    audit_dir = os.getenv("JCPR_AUDIT_DIR")
    if not audit_dir:
        raise ConfigError("JCPR_AUDIT_DIR env var required")

    mode = os.getenv("JCPR_MODE", "paper").lower()
    allow_live = os.getenv("JCPR_ALLOW_LIVE", "0") == "1"
    operator_id = os.getenv("JCPR_OPERATOR_ID", "operator-jcpr")

    return RestrictedServerConfig(
        approval_db_path=Path(approval_db).resolve(),
        audit_dir=Path(audit_dir).resolve(),
        mode=mode,
        allow_live=allow_live,
        operator_id=operator_id,
        rate_limit_per_minute=_int_env("JCPR_MCP_WRITE_RATE_PM", 30),
        rate_limit_per_hour=_int_env("JCPR_MCP_WRITE_RATE_PH", 300),
        proposal_ttl_seconds=_int_env("JCPR_PROPOSAL_TTL", 300),
        execution_ttl_seconds=_int_env("JCPR_EXECUTION_TTL", 60),
        kill_switch_ttl_seconds=_int_env("JCPR_KILL_SWITCH_TTL", 60),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_no_secrets_in_env() -> None:
    """Reject startup if any secret-bearing keyword is present in env."""
    for key in os.environ:
        upper = key.upper()
        for kw in SECRET_KEYWORDS:
            if kw in upper and not upper.startswith("JCPR_"):
                # Allow JCPR_-prefixed so we don't false-flag e.g. JCPR_TOKEN_TTL
                # but still block raw 'API_KEY', 'PASSWORD', etc.
                continue
        if upper in SECRET_KEYWORDS:
            raise ConfigError(
                f"forbidden secret env var detected: {key!r}; "
                "secrets must be loaded from .env via SecretValue, never from "
                "top-level environment"
            )


def _reject_legacy_approval_vars() -> None:
    """Phase 2 migration aid — refuse to start with legacy split paths."""
    legacy = ("JCPR_MCP_APPROVAL_DB", "JCPR_EXEC_APPROVAL_DB")
    for name in legacy:
        if os.getenv(name):
            raise ConfigError(
                f"legacy env var {name} is no longer supported (Phase 2). "
                f"Set JCPR_APPROVAL_DB to the unified data/approvals.sqlite path "
                f"and remove {name} from your environment."
            )


def _optional_path(env_name: str) -> Optional[Path]:
    val = os.getenv(env_name)
    return Path(val).resolve() if val else None


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"env var {name!r} must be integer, got {raw!r}")


# ---------------------------------------------------------------------------
# Naming alias
# ---------------------------------------------------------------------------

# Alias for naming consistency. Both names refer to the same dataclass —
# `ServerConfig` is the simpler name preferred by integration tests and
# top-level wiring (e.g. tests/integration/test_phase2b_end_to_end.py);
# `RestrictedServerConfig` is retained for callers that explicitly
# distinguish between read-only and restricted variants. Either may be
# imported, instantiated, or used in isinstance() checks — they are the
# same class.
ServerConfig = RestrictedServerConfig
