"""Core license-file management — format check, persistence, honest status."""

from __future__ import annotations

import base64
import json

import pytest
from backend import entitlements, licensing
from backend.config import settings


def _token(payload: dict) -> str:
    head = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(b"not-a-real-signature").decode().rstrip("=")
    return f"{head}.{sig}"


@pytest.fixture
def clean(tmp_path, monkeypatch):
    # licensing.license_path() resolves next to settings.db_path — point it at
    # a temp dir so tests never touch a real data/.serin-license.
    monkeypatch.setattr(settings, "db_path", tmp_path / "serin.db")
    monkeypatch.delenv("SERIN_LICENSE_KEY", raising=False)
    entitlements.set_verifier(None)
    yield
    entitlements.set_verifier(None)


def test_install_rejects_garbage(clean):
    for bad in ("", "   ", "no-dot", "too.many.dots", ".", "abc."):
        with pytest.raises(ValueError):
            licensing.install_license(bad)
    assert not licensing.license_path().exists()


def test_install_writes_file_and_reports_pending_without_pack(clean):
    key = _token({"email": "a@b.com", "plan": "intelligence", "exp": "2026-08-05"})
    out = licensing.install_license(key)
    # File saved, but with no pack loaded nothing is actually active — and the
    # status must say so honestly rather than implying success.
    assert licensing.license_path().read_text() == key
    assert out["installed"] is True
    assert out["source"] == "file"
    assert out["pack_loaded"] is False
    assert out["active"] is False
    assert out["plan"] == "opensource"
    assert out["claimed"] == {"email": "a@b.com", "plan": "intelligence", "exp": "2026-08-05"}


def test_status_active_when_pack_verifies(clean):
    licensing.install_license(_token({"email": "a@b.com", "plan": "intelligence"}))
    entitlements.set_verifier(lambda: {"plan": "intelligence", "features": ["xray"]})
    out = licensing.status()
    assert out["pack_loaded"] is True
    assert out["active"] is True
    assert out["plan"] == "intelligence"
    assert out["features"] == ["xray"]


def test_env_override_reported(clean, monkeypatch):
    monkeypatch.setenv("SERIN_LICENSE_KEY", _token({"plan": "intelligence"}))
    out = licensing.status()
    assert out["installed"] is True
    assert out["source"] == "env"


def test_clear_is_idempotent(clean):
    licensing.clear_license()  # nothing there yet — must not raise
    licensing.install_license(_token({"plan": "intelligence"}))
    assert licensing.license_path().exists()
    out = licensing.clear_license()
    assert not licensing.license_path().exists()
    assert out["installed"] is False


# --- pack install (key redeems the tarball) ---------------------------------


def _tarball(members: dict[str, bytes]) -> bytes:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _FakeResp:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content


def test_install_pack_downloads_extracts_and_saves(clean, monkeypatch):
    import httpx
    from backend import plugins

    monkeypatch.setattr(settings, "billing_url", "https://billing.example")
    key = _token({"email": "a@b.com", "plan": "intelligence"})
    tar = _tarball({"serin_pro/__init__.py": b"# pack\n", "serin_pro/xray.py": b"x = 1\n"})

    captured = {}

    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        captured["url"] = url
        captured["auth"] = (headers or {}).get("authorization")
        return _FakeResp(200, tar)

    monkeypatch.setattr(httpx, "get", fake_get)

    out = licensing.install_pack(key)
    assert out["installed"] is True and out["restart_required"] is True
    assert captured["url"] == "https://billing.example/pack/download"
    assert captured["auth"] == f"Bearer {key}"
    # extracted into the app-installed plugin dir, and the key saved
    pack_init = plugins.installed_pack_dir() / "serin_pro" / "__init__.py"
    assert pack_init.read_text() == "# pack\n"
    assert licensing.license_path().read_text() == key


def test_install_pack_rejects_bad_key_and_missing_billing(clean, monkeypatch):
    monkeypatch.setattr(settings, "billing_url", "https://billing.example")
    with pytest.raises(ValueError):
        licensing.install_pack("garbage")
    # valid key but no billing url
    monkeypatch.setattr(settings, "billing_url", "")
    with pytest.raises(ValueError):
        licensing.install_pack(_token({"plan": "intelligence"}))


def test_install_pack_maps_401_to_value_error(clean, monkeypatch):
    import httpx

    monkeypatch.setattr(settings, "billing_url", "https://billing.example")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(401))
    with pytest.raises(ValueError):
        licensing.install_pack(_token({"plan": "intelligence"}))


def test_safe_extract_rejects_traversal(clean, monkeypatch):
    import httpx

    monkeypatch.setattr(settings, "billing_url", "https://billing.example")
    evil = _tarball({"../escape.py": b"pwned\n"})
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(200, evil))
    with pytest.raises(RuntimeError):
        licensing.install_pack(_token({"plan": "intelligence"}))


def test_installed_pack_dir_loads_as_fallback(clean, monkeypatch):
    """A pack dropped in <data>/plugins loads even with no SERIN_PLUGINS_DIR."""
    from backend import plugins

    monkeypatch.setattr(settings, "plugins_dir", "")
    pack = plugins.installed_pack_dir() / "demo_plugin"
    pack.mkdir(parents=True)
    (pack / "__init__.py").write_text("LOADED = True\n")
    result = plugins.load_external_plugins()
    assert "demo_plugin" in result["loaded"]
