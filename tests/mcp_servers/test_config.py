"""
tests/mcp_servers/test_config.py — JCPR-ts-v01 (Phase 2-A)
==========================================================

`src/mcp_servers/_config.py`의 단일 환경변수 정책 + live 이중 가드 검증.

검증 항목:
1. 기본값 — JCPR_APPROVAL_DB 미설정 시 data/approvals.sqlite 사용
2. 절대 경로 환경변수 사용
3. 상대 경로는 project_root 기준으로 해석
4. mode 기본값 paper
5. live 이중 가드: --prod + JCPR_ALLOW_LIVE=1 + mode=live 셋 다 필요
6. 폐기된 환경변수(JCPR_APPROVAL_DB_MCP / _EXEC) 검출
7. 잘못된 mode 값 거부
8. 부모 디렉터리 자동 생성
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from src.mcp_servers._config import (
    ServerConfig,
    load_config,
    DEFAULT_DB_RELATIVE,
    DEPRECATED_ENV_KEYS,
    ENV_APPROVAL_DB,
    ENV_ALLOW_LIVE,
    ENV_MODE,
    _detect_deprecated_env,
)


class TestDefaults:
    def test_default_db_path(self, tmp_path: Path):
        cfg = load_config(project_root=tmp_path, overrides={})
        expected = (tmp_path / DEFAULT_DB_RELATIVE).resolve()
        assert cfg.approval_db_path == expected
        assert cfg.mode == "paper"
        assert cfg.allow_live is False

    def test_default_creates_parent_dir(self, tmp_path: Path):
        cfg = load_config(project_root=tmp_path, overrides={})
        assert cfg.approval_db_path.parent.exists()


class TestEnvOverrides:
    def test_absolute_path_env(self, tmp_path: Path):
        custom = tmp_path / "custom" / "my.sqlite"
        cfg = load_config(
            project_root=tmp_path,
            overrides={ENV_APPROVAL_DB: str(custom)},
        )
        assert cfg.approval_db_path == custom.resolve()
        # 부모 디렉터리 자동 생성
        assert custom.parent.exists()

    def test_relative_path_env(self, tmp_path: Path):
        cfg = load_config(
            project_root=tmp_path,
            overrides={ENV_APPROVAL_DB: "var/approvals.db"},
        )
        assert cfg.approval_db_path == (tmp_path / "var" / "approvals.db").resolve()


class TestLiveModeGuards:
    def test_paper_default(self, tmp_path):
        cfg = load_config(project_root=tmp_path, overrides={})
        assert cfg.mode == "paper"
        assert cfg.allow_live is False

    def test_live_requires_all_three(self, tmp_path):
        # 셋 다 만족
        cfg = load_config(
            project_root=tmp_path,
            cli_prod_flag=True,
            overrides={ENV_MODE: "live", ENV_ALLOW_LIVE: "1"},
        )
        assert cfg.mode == "live"
        assert cfg.allow_live is True

    def test_live_missing_cli_prod_downgrades(self, tmp_path, capsys):
        cfg = load_config(
            project_root=tmp_path,
            cli_prod_flag=False,  # 누락
            overrides={ENV_MODE: "live", ENV_ALLOW_LIVE: "1"},
        )
        assert cfg.mode == "paper"
        assert cfg.allow_live is False
        # 안전장치 경고 출력 확인
        err = capsys.readouterr().err
        assert "downgrade" in err.lower() or "paper" in err.lower()

    def test_live_missing_env_downgrades(self, tmp_path, capsys):
        cfg = load_config(
            project_root=tmp_path,
            cli_prod_flag=True,
            overrides={ENV_MODE: "live"},  # ALLOW_LIVE 누락
        )
        assert cfg.mode == "paper"
        assert cfg.allow_live is False

    def test_live_missing_mode_env_stays_paper(self, tmp_path):
        cfg = load_config(
            project_root=tmp_path,
            cli_prod_flag=True,
            overrides={ENV_ALLOW_LIVE: "1"},  # mode 미지정 → paper
        )
        assert cfg.mode == "paper"

    def test_invalid_mode_value(self, tmp_path):
        with pytest.raises(ValueError, match="paper.*live"):
            load_config(
                project_root=tmp_path,
                overrides={ENV_MODE: "production"},
            )


class TestDeprecatedEnvDetection:
    @pytest.mark.parametrize("key", list(DEPRECATED_ENV_KEYS))
    def test_each_deprecated_key_warns(self, tmp_path, monkeypatch, capsys, key):
        # os.environ 직접 세팅 — _detect_deprecated_env가 os.environ을 봄
        monkeypatch.setenv(key, "/some/path.sqlite")
        load_config(project_root=tmp_path, overrides={})
        err = capsys.readouterr().err
        assert key in err
        assert "deprecated" in err.lower() or "폐기" in err

    def test_no_deprecated_no_warning(self, tmp_path, monkeypatch, capsys):
        for k in DEPRECATED_ENV_KEYS:
            monkeypatch.delenv(k, raising=False)
        load_config(project_root=tmp_path, overrides={})
        err = capsys.readouterr().err
        for k in DEPRECATED_ENV_KEYS:
            assert k not in err

    def test_detect_returns_list(self, monkeypatch):
        for k in DEPRECATED_ENV_KEYS:
            monkeypatch.delenv(k, raising=False)
        # 가짜 stderr writer로 출력 흡수
        sink = io.StringIO()
        result = _detect_deprecated_env(stderr_writer=sink.write)
        assert result == []

        monkeypatch.setenv(DEPRECATED_ENV_KEYS[0], "/x")
        result2 = _detect_deprecated_env(stderr_writer=sink.write)
        assert DEPRECATED_ENV_KEYS[0] in result2


class TestServerConfigInvariants:
    def test_relative_path_rejected(self):
        with pytest.raises(ValueError, match="absolute"):
            ServerConfig(approval_db_path=Path("data/x.sqlite"))

    def test_invalid_mode_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="paper.*live"):
            ServerConfig(
                approval_db_path=tmp_path / "x.sqlite",
                mode="trading",  # type: ignore[arg-type]
            )

    def test_live_without_allow_live_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="allow_live"):
            ServerConfig(
                approval_db_path=tmp_path / "x.sqlite",
                mode="live",
                allow_live=False,
            )

    def test_live_with_allow_live_ok(self, tmp_path):
        cfg = ServerConfig(
            approval_db_path=tmp_path / "x.sqlite",
            mode="live",
            allow_live=True,
        )
        assert cfg.mode == "live"
