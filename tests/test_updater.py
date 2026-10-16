"""The update channel must fail closed before executing downloaded code."""
import hashlib
from pathlib import Path

import pytest

from apexity_bridge import updater


class Response:
    def __init__(self, data=None, content=b"installer"):
        self.data = data
        self.content = content
        self.url = "https://release-assets.githubusercontent.com/asset"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    def json(self):
        return self.data

    def iter_content(self, size):
        yield self.content


def manifest(version="1.0.2"):
    return dict(version=version,
                url=f"{updater.RELEASE_ROOT}v{version}/ApexityEduBridge-Setup-{version}.exe",
                sha256=hashlib.sha256(b"installer").hexdigest(), size=9)


def test_version_comparison(monkeypatch):
    monkeypatch.setattr(updater, "__version__", "1.0.9")
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: Response(manifest("1.0.10")))
    assert updater.check_update().version == "1.0.10"
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: Response(manifest("1.0.8")))
    assert updater.check_update() is None


@pytest.mark.parametrize("key,value", [("url", "https://evil.test/setup.exe"),
                                      ("sha256", "bad"), ("version", "v2"),
                                      ("size", -1), ("size", True)])
def test_reject_bad_manifest(monkeypatch, key, value):
    data = manifest()
    data[key] = value
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: Response(data))
    with pytest.raises(ValueError):
        updater.check_update()


@pytest.mark.parametrize("content", [b"installer", b"corrupted", b"short", b"too long installer"])
def test_download_verification_and_cleanup(monkeypatch, tmp_path, content):
    folder = tmp_path / "download"
    folder.mkdir()
    monkeypatch.setattr(updater.tempfile, "mkdtemp", lambda **k: str(folder))
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: Response(content=content))
    update = updater.Update(**manifest())
    if content == b"installer":
        values = []
        assert updater.download_update(update, values.append).read_bytes() == content
        assert values[-1] == 100
    else:
        with pytest.raises(ValueError):
            updater.download_update(update)
        assert not folder.exists()


def test_source_install_not_supported():
    with pytest.raises(RuntimeError):
        updater.launch_update(Path("setup.exe"))
