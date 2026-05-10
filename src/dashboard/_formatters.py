"""Display formatters for the JCPR dashboard.

Pure functions converting Decimal/datetime/enum values into the
canonical Korean-format strings the dashboard renders. All functions
return strings; none mutates inputs. Designed to compose with
_security.scrub_secrets for defense-in-depth on text that originates
outside this package.

Conventions:
    - KRW amounts: thousands-grouped, integer rounding for display
      (raw Decimal preserved for calculations elsewhere).
    - Datetimes: convert UTC to KST (Asia/Seoul, +09:00) for display.
    - Modes: paper/live colored by caller using the returned token.
    - Severity: ok/minor/major from ReconciliationReport.severity().
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional


__all__ = [
    "KST",
    "format_krw",
    "format_pct",
    "format_bps",
    "format_datetime_kst",
    "format_mode_label",
    "format_severity_label",
    "format_decimal_signed",
]


# Asia/Seoul fixed offset. Pure +09:00, no DST (Korea has no DST).
# Avoiding zoneinfo dependency keeps this module testable without
# system tz database.
KST = timezone(timedelta(hours=9), name="KST")


def format_krw(amount: Optional[Decimal | int | str], *, signed: bool = False) -> str:
    """Format a KRW amount with thousand-separators and ' KRW' suffix.

    Args:
        amount: Decimal/int/str to format; None becomes '—'.
        signed: when True, positive amounts get an explicit '+' prefix.

    Returns:
        e.g. '1,234,567 KRW' or '+1,234,567 KRW' or '—' for None.

    Strings that fail to parse as Decimal also return '—' (graceful
    degrade rather than raise — formatters never crash the UI).
    """
    if amount is None:
        return "—"
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return "—"
    # Round to integer KRW for display; raw value preserved for math
    # elsewhere. Use int() after Decimal quantize for sign-correct truncation.
    rounded = int(value)
    sign = ""
    if signed and rounded > 0:
        sign = "+"
    return f"{sign}{rounded:,} KRW"


def format_pct(fraction: Optional[Decimal | float | str], *, decimals: int = 2) -> str:
    """Format a 0..1 fraction (or already-percent number) as 'X.XX%'.

    Args:
        fraction: 0.1234 → '12.34%'; 12.34 (>1) is treated as already-percent.
        decimals: digits after the decimal point.

    Returns:
        Formatted percent string, '—' for None / unparseable.
    """
    if fraction is None:
        return "—"
    try:
        value = Decimal(str(fraction))
    except (InvalidOperation, ValueError):
        return "—"
    # Heuristic: if abs(value) <= 1, treat as fraction; otherwise as percent.
    # This matches both pnl_engine outputs (fractions) and capacity.yaml
    # (raw percent like 20.0).
    if abs(value) <= 1:
        value = value * 100
    return f"{value:.{decimals}f}%"


def format_bps(bps: Optional[Decimal | float | str], *, decimals: int = 2) -> str:
    """Format a basis-points value as 'X.XX bps' (signed)."""
    if bps is None:
        return "—"
    try:
        value = Decimal(str(bps))
    except (InvalidOperation, ValueError):
        return "—"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.{decimals}f} bps"


def format_datetime_kst(dt: Optional[datetime]) -> str:
    """Convert a UTC datetime to KST and format as 'YYYY-MM-DD HH:MM:SS KST'.

    Args:
        dt: tz-aware UTC datetime; naive datetimes are treated as UTC
            with a warning-free fallback (test fixtures sometimes use naive).

    Returns:
        Formatted string in KST, or '—' for None.
    """
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    kst_dt = dt.astimezone(KST)
    return kst_dt.strftime("%Y-%m-%d %H:%M:%S KST")


def format_mode_label(mode: Optional[str]) -> str:
    """Return a display label for a mode string.

    Returns 'PAPER' / 'LIVE' (uppercase, fixed). Caller selects
    color/style based on this label. Unknown modes return '?' to
    signal a config issue without crashing the UI.
    """
    if not mode:
        return "?"
    m = mode.strip().lower()
    if m == "paper":
        return "PAPER"
    if m == "live":
        return "LIVE"
    return "?"


def format_severity_label(severity: Optional[str]) -> str:
    """Return a display label for a ReconciliationReport severity.

    Returns 'OK' / 'MINOR' / 'MAJOR' / '?'. Caller chooses color.
    Mapping is intentionally case-insensitive — Reconciler returns
    lowercase, but operator-facing strings are uppercased here for
    visual prominence.
    """
    if not severity:
        return "?"
    s = severity.strip().lower()
    if s == "ok":
        return "OK"
    if s == "minor":
        return "MINOR"
    if s == "major":
        return "MAJOR"
    return "?"


def format_decimal_signed(
    value: Optional[Decimal | int | str],
    *,
    decimals: int = 0,
) -> str:
    """Format a Decimal with explicit sign ('+' or '-' prefix).

    Used for P&L deltas where positive/negative directionality is
    semantically important (gain vs loss). Zero displays as '0' (no sign).

    Args:
        value: numeric to format.
        decimals: digits after decimal point; 0 means integer display.

    Returns:
        e.g. '+1,234' / '-1,234' / '0' / '—'.
    """
    if value is None:
        return "—"
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return "—"
    if d == 0:
        return "0" if decimals == 0 else f"0.{'0' * decimals}"
    sign = "+" if d > 0 else ""
    if decimals == 0:
        return f"{sign}{int(d):,}"
    return f"{sign}{d:,.{decimals}f}"
