"""Security primitives: password hashing, secrets, log redaction, singleton."""
from __future__ import annotations

import logging
import os

import pytest

from apexity_bridge.logging_utils import _redact
from apexity_bridge.security.passwords import hash_password, verify_password
from apexity_bridge.security.secrets import SecretStore
from apexity_bridge.windows_service.singleton import SingleInstance


def test_password_hash_roundtrip():
    hashed = hash_password("s3cure-school-pw")
    assert verify_password("s3cure-school-pw", hashed)
    assert not verify_password("wrong-password", hashed)
    assert not verify_password("s3cure-school-pw", "garbage")


def test_password_minimum_length():
    with pytest.raises(ValueError):
        hash_password("short")


def test_password_hash_unique_salt():
    assert hash_password("same-password") != hash_password("same-password")


def test_secret_store_file_fallback(tmp_path):
    store = SecretStore(tmp_path)
    store.set_ems_token("tok-abc-123")
    assert store.get_ems_token() == "tok-abc-123"
    assert store.has_ems_token()
    store.delete_ems_token()
    assert not store.has_ems_token()
    if store._backend.__class__.__name__ == "_FileBackend":
        mode = os.stat(tmp_path / "secrets.json").st_mode & 0o777
        assert mode == 0o600


def test_secret_store_in_memory():
    store = SecretStore.in_memory(token="x")
    assert store.get_ems_token() == "x"


def test_log_redaction_masks_tokens():
    assert "abc123" not in _redact("Authorization: Bearer abc123")
    assert "tok99" not in _redact("token=tok99 failed")
    assert "hunter2" not in _redact("password: hunter2")
    assert "normal message" in _redact("normal message")


def test_secrets_never_in_settings_dump(repos):
    repos["settings"].set("my_key", "secret-value", is_secret=True)
    assert repos["settings"].all_public()["my_key"] == "***"


def test_single_instance_guard(repos):
    first = SingleInstance()
    assert first.acquire()
    second = SingleInstance()
    assert not second.acquire()           # duplicate prevented
    first.release()
    third = SingleInstance()
    assert third.acquire()                # released lock can be re-taken
    third.release()


def test_config_refuses_plaintext_http_remote(tmp_path):
    from apexity_bridge.config import Config
    cfg = Config(ems_base_url="http://school.example.com", db_path=tmp_path / "b.db")
    errors = cfg.validate()
    assert any("plaintext" in e.lower() or "HTTPS" in e for e in errors)


def test_config_refuses_open_dashboard_bind(tmp_path):
    from apexity_bridge.config import Config
    cfg = Config(dashboard_host="0.0.0.0", dashboard_allow_lan=False,
                 db_path=tmp_path / "b.db")
    assert any("LAN" in e or "0.0.0.0" in e for e in cfg.validate())


def test_config_allows_localhost_fixture(tmp_path):
    from apexity_bridge.config import Config
    cfg = Config(ems_base_url="http://127.0.0.1:8080",
                 ems_allow_insecure_localhost=True, db_path=tmp_path / "b.db")
    assert cfg.validate() == []


def test_secret_store_read_failure_is_reported_not_raised(tmp_path):
    class _BrokenBackend:
        def get(self, _service):
            raise RuntimeError("Credential Manager is locked")

    store = SecretStore(tmp_path)
    store._backend = _BrokenBackend()

    assert store.get_ems_token() is None          # Settings page stays alive
    assert store.has_ems_token() is False
    assert "locked" in (store.last_error or "")   # but the reason is visible
