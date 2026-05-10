"""Sidebar defaults 자동 로더 — 단위 테스트.

Test categories:
    - env 우선 (env > yaml > default)
    - yaml fallback (env 미설정 시 yaml)
    - default fallback (env + yaml 미설정 시)
    - 출처 추적 (sources 매핑)
    - capacity.local.yaml 미존재
    - capacity.local.yaml 파싱 실패 (silent)
    - 자본 정보 yaml 추출
    - current_cash_krw 키 부재
    - 음수 / 비숫자 자본 → 0
    - immutability (frozen dataclass)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from src.dashboard._sidebar_defaults import (
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_YAML,
    SidebarDefaults,
    resolve_sidebar_defaults,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch) -> Iterator[None]:
    """JCPR_* 환경변수 모두 제거 — 테스트 격리."""
    keys_to_clear = [
        "JCPR_POSITIONS_DB",
        "JCPR_OHLCV_DB",
        "JCPR_QUOTE_DB",
        "JCPR_RISK_AUDIT",
        "JCPR_EXEC_AUDIT",
        "JCPR_RECON_AUDIT",
        "JCPR_REJECTION_REPORT",
        "JCPR_KILL_SWITCH",
        "JCPR_DASHBOARD_CAPACITY_YAML",
    ]
    for key in keys_to_clear:
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def fake_yaml(tmp_path) -> Path:
    """간이 capacity.local.yaml — capital_caps 정상."""
    yaml_path = tmp_path / "configs" / "capacity.local.yaml"
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(
        """
capital_caps:
  total_deployed_capital:
    amount: 10000000
    unit: KRW
  current_cash_krw: 8000000
""",
        encoding="utf-8",
    )
    return yaml_path


# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------


class TestPathResolution:
    def test_env_takes_precedence(self, monkeypatch, tmp_path, clean_env):
        custom = "/tmp/custom_positions.sqlite"
        monkeypatch.setenv("JCPR_POSITIONS_DB", custom)

        defaults = resolve_sidebar_defaults(project_root=tmp_path)
        assert defaults.positions_db == custom
        assert defaults.sources["positions_db"] == SOURCE_ENV

    def test_no_env_uses_default(self, tmp_path, clean_env):
        defaults = resolve_sidebar_defaults(project_root=tmp_path)
        expected = str(tmp_path.resolve() / "data" / "positions.sqlite")
        assert defaults.positions_db == expected
        assert defaults.sources["positions_db"] == SOURCE_DEFAULT

    def test_empty_env_treated_as_unset(self, monkeypatch, tmp_path, clean_env):
        monkeypatch.setenv("JCPR_OHLCV_DB", "")
        defaults = resolve_sidebar_defaults(project_root=tmp_path)
        # 빈 문자열 → default fallback
        assert defaults.ohlcv_db.endswith("ohlcv.sqlite")
        assert defaults.sources["ohlcv_db"] == SOURCE_DEFAULT

    def test_whitespace_env_treated_as_unset(self, monkeypatch, tmp_path, clean_env):
        monkeypatch.setenv("JCPR_QUOTE_DB", "   ")
        defaults = resolve_sidebar_defaults(project_root=tmp_path)
        assert defaults.sources["quote_db"] == SOURCE_DEFAULT

    def test_all_audit_paths_use_data_audit_dir(self, tmp_path, clean_env):
        defaults = resolve_sidebar_defaults(project_root=tmp_path)
        for path in (
            defaults.risk_audit_path,
            defaults.execution_audit_path,
            defaults.reconciliation_audit_path,
            defaults.rejection_report_path,
        ):
            assert "data/audit" in path or "data\\audit" in path

    def test_kill_switch_uses_runtime_dir(self, tmp_path, clean_env):
        defaults = resolve_sidebar_defaults(project_root=tmp_path)
        assert "runtime" in defaults.kill_switch_file
        assert defaults.kill_switch_file.endswith("KILL_SWITCH_ON")

    def test_capacity_config_default_path(self, tmp_path, clean_env):
        defaults = resolve_sidebar_defaults(project_root=tmp_path)
        assert defaults.capacity_config.endswith("capacity.local.yaml")
        assert "configs" in defaults.capacity_config


# ---------------------------------------------------------------------------
# Capital info from yaml
# ---------------------------------------------------------------------------


class TestCapitalFromYaml:
    def test_yaml_provides_starting_and_cash(self, monkeypatch, tmp_path, fake_yaml, clean_env):
        monkeypatch.setenv("JCPR_DASHBOARD_CAPACITY_YAML", str(fake_yaml))
        defaults = resolve_sidebar_defaults(project_root=tmp_path)
        assert defaults.starting_capital_krw == 10_000_000.0
        assert defaults.cash_krw == 8_000_000.0
        assert defaults.sources["starting_capital_krw"] == SOURCE_YAML
        assert defaults.sources["cash_krw"] == SOURCE_YAML

    def test_yaml_missing_falls_back_to_zero(self, tmp_path, clean_env):
        # yaml 미존재
        defaults = resolve_sidebar_defaults(project_root=tmp_path)
        assert defaults.starting_capital_krw == 0.0
        assert defaults.cash_krw == 0.0
        assert defaults.sources["starting_capital_krw"] == SOURCE_DEFAULT

    def test_yaml_without_current_cash_krw(self, monkeypatch, tmp_path, clean_env):
        yaml_path = tmp_path / "configs" / "capacity.local.yaml"
        yaml_path.parent.mkdir(parents=True)
        yaml_path.write_text(
            """
capital_caps:
  total_deployed_capital:
    amount: 5000000
    unit: KRW
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("JCPR_DASHBOARD_CAPACITY_YAML", str(yaml_path))
        defaults = resolve_sidebar_defaults(project_root=tmp_path)
        assert defaults.starting_capital_krw == 5_000_000.0
        assert defaults.cash_krw == 0.0
        # starting > 0 이면 yaml source
        assert defaults.sources["starting_capital_krw"] == SOURCE_YAML

    def test_yaml_malformed_returns_zero_silently(self, monkeypatch, tmp_path, clean_env):
        yaml_path = tmp_path / "configs" / "capacity.local.yaml"
        yaml_path.parent.mkdir(parents=True)
        yaml_path.write_text("not: valid: yaml: ::: [", encoding="utf-8")
        monkeypatch.setenv("JCPR_DASHBOARD_CAPACITY_YAML", str(yaml_path))

        # silent 동작 — exception 없이 0 반환
        defaults = resolve_sidebar_defaults(project_root=tmp_path)
        assert defaults.starting_capital_krw == 0.0
        assert defaults.cash_krw == 0.0
        assert defaults.sources["starting_capital_krw"] == SOURCE_DEFAULT

    def test_yaml_with_negative_amount_returns_zero(self, monkeypatch, tmp_path, clean_env):
        yaml_path = tmp_path / "configs" / "capacity.local.yaml"
        yaml_path.parent.mkdir(parents=True)
        yaml_path.write_text(
            """
capital_caps:
  total_deployed_capital:
    amount: -1000
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("JCPR_DASHBOARD_CAPACITY_YAML", str(yaml_path))
        defaults = resolve_sidebar_defaults(project_root=tmp_path)
        assert defaults.starting_capital_krw == 0.0

    def test_yaml_with_string_amount_silently_zero(self, monkeypatch, tmp_path, clean_env):
        yaml_path = tmp_path / "configs" / "capacity.local.yaml"
        yaml_path.parent.mkdir(parents=True)
        yaml_path.write_text(
            """
capital_caps:
  total_deployed_capital:
    amount: "not_a_number"
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("JCPR_DASHBOARD_CAPACITY_YAML", str(yaml_path))
        defaults = resolve_sidebar_defaults(project_root=tmp_path)
        assert defaults.starting_capital_krw == 0.0


# ---------------------------------------------------------------------------
# Sources mapping & immutability
# ---------------------------------------------------------------------------


class TestSourcesAndImmutability:
    def test_sources_contains_all_fields(self, tmp_path, clean_env):
        defaults = resolve_sidebar_defaults(project_root=tmp_path)
        expected_keys = {
            "positions_db", "ohlcv_db", "quote_db",
            "risk_audit_path", "execution_audit_path",
            "reconciliation_audit_path", "rejection_report_path",
            "kill_switch_file", "capacity_config",
            "starting_capital_krw", "cash_krw",
        }
        assert set(defaults.sources.keys()) == expected_keys

    def test_sources_values_are_known_identifiers(self, tmp_path, clean_env):
        defaults = resolve_sidebar_defaults(project_root=tmp_path)
        valid = {SOURCE_ENV, SOURCE_YAML, SOURCE_DEFAULT}
        for src in defaults.sources.values():
            assert src in valid, f"unknown source: {src!r}"

    def test_defaults_is_frozen(self, tmp_path, clean_env):
        defaults = resolve_sidebar_defaults(project_root=tmp_path)
        with pytest.raises((AttributeError, Exception)):
            defaults.positions_db = "/changed"  # type: ignore[misc]

    def test_project_root_none_uses_cwd(self, monkeypatch, tmp_path, clean_env):
        # project_root=None → CWD 사용
        monkeypatch.chdir(tmp_path)
        defaults = resolve_sidebar_defaults(project_root=None)
        assert str(tmp_path.resolve()) in defaults.positions_db
