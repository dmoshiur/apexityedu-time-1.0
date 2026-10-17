"""Secret storage — Windows Credential Manager first, never plaintext config.

* On Windows, ``keyring`` uses Credential Manager (per-user, OS-encrypted).
* If no real backend exists (e.g. headless CI/dev on Linux), we fall back to a
  chmod 0600 file in the data dir and *log a loud warning* — acceptable only
  for development; the installer uses the Windows backend.
* Secrets are never written into .env, the SQLite DB, or logs.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("apexity_bridge.security.secrets")

SERVICE_EMS_TOKEN = "apexityedu-bridge/ems-device-token"

_USERNAME = "apexity-bridge"


class SecretStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self._backend = self._select_backend()

    # ---------------------------------------------------------------- backend --
    def _select_backend(self):
        try:
            import keyring
            from keyring.backends.fail import Keyring as FailKeyring

            backend = keyring.get_keyring()
            if isinstance(backend, FailKeyring) or "fail" in type(backend).__module__.lower():
                raise RuntimeError("no usable keyring backend")
            # smoke test
            backend.get_password("apexityedu-bridge/self-test", _USERNAME)
            log.info("secret backend: OS credential store (%s)", type(backend).__name__)
            return _KeyringBackend(backend)
        except Exception as exc:
            log.warning(
                "OS credential store unavailable (%s). Falling back to a "
                "0600-permission file under the data dir — acceptable for "
                "development ONLY. On the production Windows PC the installer "
                "stores secrets in Windows Credential Manager.",
                exc,
            )
            return _FileBackend(self.data_dir / "secrets.json")

    # ------------------------------------------------------------------- API ---
    @property
    def backend_name(self) -> str:
        return type(self._backend).__name__.lstrip("_")

    def set_ems_token(self, token: str) -> None:
        # A failed *write* must raise: claiming "saved" while the Credential
        # Manager refused the secret would be a lie. Reads are different — a
        # broken backend must not take the whole Settings page down.
        self._backend.set(SERVICE_EMS_TOKEN, token)

    def get_ems_token(self) -> str | None:
        try:
            return self._backend.get(SERVICE_EMS_TOKEN)
        except Exception as exc:          # keyring backend error / locked vault
            log.error("could not read the EMS token from the credential store: %s", exc)
            return None

    def delete_ems_token(self) -> None:
        self._backend.delete(SERVICE_EMS_TOKEN)

    def has_ems_token(self) -> bool:
        return bool(self.get_ems_token())

    # ------------------------------------------------------------------ tests --
    @classmethod
    def in_memory(cls, token: str | None = None) -> "SecretStore":
        store = cls.__new__(cls)
        store.data_dir = Path(":memory:")
        backend = _MemoryBackend()
        if token:
            backend.set(SERVICE_EMS_TOKEN, token)
        store._backend = backend
        return store


class _KeyringBackend:
    def __init__(self, keyring_backend):
        self._kr = keyring_backend

    def get(self, service: str) -> str | None:
        return self._kr.get_password(service, _USERNAME)

    def set(self, service: str, value: str) -> None:
        self._kr.set_password(service, _USERNAME, value)

    def delete(self, service: str) -> None:
        try:
            self._kr.delete_password(service, _USERNAME)
        except Exception:
            pass


class _FileBackend:
    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(self.path, 0o600)

    def get(self, service: str) -> str | None:
        return self._load().get(service)

    def set(self, service: str, value: str) -> None:
        data = self._load()
        data[service] = value
        self._save(data)

    def delete(self, service: str) -> None:
        data = self._load()
        if service in data:
            del data[service]
            self._save(data)


class _MemoryBackend:
    def __init__(self):
        self._data: dict[str, str] = {}

    def get(self, service: str) -> str | None:
        return self._data.get(service)

    def set(self, service: str, value: str) -> None:
        self._data[service] = value

    def delete(self, service: str) -> None:
        self._data.pop(service, None)
