"""Tests for brokers/_secrets.py — strict security verification."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from src.brokers._secrets import (
    KIS_PAPER_ENV_VARS,
    KIS_PROD_ENV_VARS,
    MASK_PREFIX_LEN,
    REQUIRED_ENV_MODE,
    KISSecrets,
    SecretLoadError,
    SecretValue,
    _mask_secret,
    load_kis_secrets,
    parse_env_file,
    verify_env_file_permissions,
    verify_gitignore_covers_env,
)


SKIP_NON_POSIX = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX-specific permission tests",
)


# =============================================================================
# SecretValue
# =============================================================================

class TestSecretValue:
    def test_reveal_returns_raw(self):
        s = SecretValue("super-secret-value-1234567890")
        assert s.reveal() == "super-secret-value-1234567890"

    def test_repr_does_not_leak(self):
        s = SecretValue("super-secret-value-1234567890")
        r = repr(s)
        assert "super-secret-value" not in r
        assert "supe" in r  # first 4 chars allowed in mask
        assert "***" in r

    def test_str_does_not_leak(self):
        s = SecretValue("super-secret-value-1234567890")
        assert "super-secret-value" not in str(s)

    def test_masked_property(self):
        s = SecretValue("ABCDEFGHIJKLMNOP")
        assert s.masked == "ABCD***"

    def test_short_secret_fully_masked(self):
        s = SecretValue("xy")
        assert s.masked == "***"
        assert "xy" not in str(s)

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            SecretValue("")

    def test_rejects_non_str(self):
        with pytest.raises(TypeError):
            SecretValue(12345)  # type: ignore

    def test_equality_compares_raw(self):
        a = SecretValue("samevalue123456")
        b = SecretValue("samevalue123456")
        c = SecretValue("differentvalue1")
        assert a == b
        assert a != c

    def test_hash_stable(self):
        a = SecretValue("hashtest1234")
        b = SecretValue("hashtest1234")
        assert hash(a) == hash(b)


class TestMaskSecret:
    def test_long_string_truncated(self):
        assert _mask_secret("ABCDEFGH") == "ABCD***"

    def test_short_string_fully_masked(self):
        assert _mask_secret("ABC") == "***"
        assert _mask_secret("AB") == "***"
        assert _mask_secret("A") == "***"

    def test_empty_returns_stars(self):
        assert _mask_secret("") == "***"


# =============================================================================
# KISSecrets validation
# =============================================================================

class TestKISSecretsValidation:
    def test_accepts_valid(self):
        s = KISSecrets(
            appkey=SecretValue("PSED" + "A" * 32),
            appsecret=SecretValue("Z" * 180),
            account_number="12345678",
            account_product="01",
            mode="paper",
        )
        assert s.account_masked == "1234***"

    def test_rejects_bad_mode(self):
        with pytest.raises(ValueError, match="mode must be"):
            KISSecrets(
                appkey=SecretValue("a" * 16),
                appsecret=SecretValue("b" * 16),
                account_number="12345678",
                account_product="01",
                mode="live",  # invalid
            )

    def test_rejects_bad_account_number(self):
        with pytest.raises(ValueError, match="8 digits"):
            KISSecrets(
                appkey=SecretValue("a" * 16),
                appsecret=SecretValue("b" * 16),
                account_number="1234567",  # only 7 digits
                account_product="01",
                mode="paper",
            )

    def test_rejects_non_digit_account(self):
        with pytest.raises(ValueError, match="8 digits"):
            KISSecrets(
                appkey=SecretValue("a" * 16),
                appsecret=SecretValue("b" * 16),
                account_number="1234abcd",
                account_product="01",
                mode="paper",
            )

    def test_rejects_bad_product_code(self):
        with pytest.raises(ValueError, match="2 digits"):
            KISSecrets(
                appkey=SecretValue("a" * 16),
                appsecret=SecretValue("b" * 16),
                account_number="12345678",
                account_product="1",  # only 1 digit
                mode="paper",
            )

    def test_appkey_masked_property(self):
        s = KISSecrets(
            appkey=SecretValue("PSED1234567890"),
            appsecret=SecretValue("X" * 100),
            account_number="12345678",
            account_product="01",
            mode="paper",
        )
        assert s.appkey_masked == "PSED***"
        assert s.appsecret_masked == "XXXX***"  # first 4 chars + ***


# =============================================================================
# parse_env_file
# =============================================================================

class TestParseEnvFile:
    def test_parses_simple(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("FOO=bar\nBAZ=qux\n", encoding="utf-8")
        result = parse_env_file(env)
        assert result == {"FOO": "bar", "BAZ": "qux"}

    def test_handles_quoted_values(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text('FOO="bar baz"\nBAZ=\'qux quux\'\n', encoding="utf-8")
        result = parse_env_file(env)
        assert result == {"FOO": "bar baz", "BAZ": "qux quux"}

    def test_ignores_comments_and_blanks(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("# comment\n\nFOO=bar\n  # indented comment\n", encoding="utf-8")
        result = parse_env_file(env)
        assert result == {"FOO": "bar"}

    def test_rejects_malformed_line(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("FOO=bar\nNOEQUALSHERE\n", encoding="utf-8")
        with pytest.raises(SecretLoadError, match="missing '='"):
            parse_env_file(env)

    def test_rejects_invalid_key(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("123BAD=value\n", encoding="utf-8")
        with pytest.raises(SecretLoadError, match="invalid env key"):
            parse_env_file(env)

    def test_rejects_lowercase_key(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("lowercase=value\n", encoding="utf-8")
        with pytest.raises(SecretLoadError, match="invalid env key"):
            parse_env_file(env)


# =============================================================================
# Permission verification (POSIX only)
# =============================================================================

@SKIP_NON_POSIX
class TestPermissions:
    def test_accepts_0600(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("FOO=bar", encoding="utf-8")
        os.chmod(env, 0o600)
        verify_env_file_permissions(env)  # no raise

    def test_rejects_0644(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("FOO=bar", encoding="utf-8")
        os.chmod(env, 0o644)
        with pytest.raises(SecretLoadError, match="insecure permissions"):
            verify_env_file_permissions(env)

    def test_rejects_0666(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("FOO=bar", encoding="utf-8")
        os.chmod(env, 0o666)
        with pytest.raises(SecretLoadError, match="insecure permissions"):
            verify_env_file_permissions(env)

    def test_rejects_missing_file(self, tmp_path):
        with pytest.raises(SecretLoadError, match="not found"):
            verify_env_file_permissions(tmp_path / "missing.env")


# =============================================================================
# Gitignore verification
# =============================================================================

class TestGitignore:
    def test_accepts_dot_env_listed(self, tmp_path):
        (tmp_path / ".gitignore").write_text(
            "venv/\n.env\n*.pyc\n", encoding="utf-8"
        )
        verify_gitignore_covers_env(tmp_path)

    def test_accepts_dot_env_with_comment(self, tmp_path):
        (tmp_path / ".gitignore").write_text(
            ".env  # secret file\n", encoding="utf-8"
        )
        verify_gitignore_covers_env(tmp_path)

    def test_rejects_missing_dot_env(self, tmp_path):
        (tmp_path / ".gitignore").write_text("venv/\n*.pyc\n", encoding="utf-8")
        with pytest.raises(SecretLoadError, match="\\.env"):
            verify_gitignore_covers_env(tmp_path)

    def test_rejects_missing_gitignore(self, tmp_path):
        with pytest.raises(SecretLoadError, match=".gitignore not found"):
            verify_gitignore_covers_env(tmp_path)

    def test_rejects_dot_env_as_substring(self, tmp_path):
        # "myproj.env" should not satisfy ".env" check
        (tmp_path / ".gitignore").write_text("myproj.env\n", encoding="utf-8")
        with pytest.raises(SecretLoadError):
            verify_gitignore_covers_env(tmp_path)


# =============================================================================
# load_kis_secrets — integration
# =============================================================================

@SKIP_NON_POSIX
class TestLoadKISSecrets:
    def _write_env(self, tmp_path: Path, mode: str = "paper") -> Path:
        env = tmp_path / ".env"
        if mode == "paper":
            env.write_text(
                "KIS_PAPER_APPKEY=PSED" + "A" * 32 + "\n"
                "KIS_PAPER_APPSECRET=" + "B" * 180 + "\n"
                "KIS_PAPER_ACCOUNT=12345678\n"
                "KIS_PAPER_ACCOUNT_PRODUCT=01\n",
                encoding="utf-8",
            )
        else:
            env.write_text(
                "KIS_PROD_APPKEY=PSED" + "C" * 32 + "\n"
                "KIS_PROD_APPSECRET=" + "D" * 180 + "\n"
                "KIS_PROD_ACCOUNT=87654321\n"
                "KIS_PROD_ACCOUNT_PRODUCT=01\n",
                encoding="utf-8",
            )
        os.chmod(env, 0o600)
        (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
        return env

    def test_loads_paper_secrets(self, tmp_path):
        env = self._write_env(tmp_path, "paper")
        s = load_kis_secrets(env_path=env, mode="paper")
        assert s.mode == "paper"
        assert s.account_number == "12345678"
        assert s.appkey.reveal().startswith("PSED")

    def test_loads_prod_secrets(self, tmp_path):
        env = self._write_env(tmp_path, "prod")
        s = load_kis_secrets(env_path=env, mode="prod")
        assert s.mode == "prod"
        assert s.account_number == "87654321"

    def test_rejects_wrong_perms(self, tmp_path):
        env = self._write_env(tmp_path, "paper")
        os.chmod(env, 0o644)
        with pytest.raises(SecretLoadError, match="insecure permissions"):
            load_kis_secrets(env_path=env, mode="paper")

    def test_rejects_missing_var(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "KIS_PAPER_APPKEY=" + "A" * 36 + "\n"
            "KIS_PAPER_ACCOUNT=12345678\n"
            "KIS_PAPER_ACCOUNT_PRODUCT=01\n",
            encoding="utf-8",
        )
        os.chmod(env, 0o600)
        (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
        with pytest.raises(SecretLoadError, match="missing required"):
            load_kis_secrets(env_path=env, mode="paper")

    def test_rejects_placeholder(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "KIS_PAPER_APPKEY=YOUR_APPKEY_HERE\n"
            "KIS_PAPER_APPSECRET=" + "B" * 180 + "\n"
            "KIS_PAPER_ACCOUNT=12345678\n"
            "KIS_PAPER_ACCOUNT_PRODUCT=01\n",
            encoding="utf-8",
        )
        os.chmod(env, 0o600)
        (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
        with pytest.raises(SecretLoadError, match="placeholder"):
            load_kis_secrets(env_path=env, mode="paper")

    def test_rejects_missing_gitignore(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "KIS_PAPER_APPKEY=" + "A" * 36 + "\n"
            "KIS_PAPER_APPSECRET=" + "B" * 180 + "\n"
            "KIS_PAPER_ACCOUNT=12345678\n"
            "KIS_PAPER_ACCOUNT_PRODUCT=01\n",
            encoding="utf-8",
        )
        os.chmod(env, 0o600)
        # No .gitignore created
        with pytest.raises(SecretLoadError, match="gitignore"):
            load_kis_secrets(env_path=env, mode="paper")

    def test_rejects_bad_mode(self, tmp_path):
        env = self._write_env(tmp_path, "paper")
        with pytest.raises(SecretLoadError, match="mode must be"):
            load_kis_secrets(env_path=env, mode="invalid")
