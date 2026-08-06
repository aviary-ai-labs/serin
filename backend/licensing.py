"""License-file management for the open-core seam.

Core cannot *verify* a license — only the commercial pack embeds the public
key. What core does is manage the license *file* the pack reads: accept a
pasted key (syntactic check only), write it to ``data/.serin-license``, and
report status. The pack's verifier re-reads the file on every check, so a
freshly pasted key activates without a restart *when the pack is loaded*.

If no pack is installed, a valid-looking key still saves but nothing unlocks —
:func:`status` says so plainly rather than implying success.
"""

from __future__ import annotations

import base64
import binascii
import json

from backend import entitlements
from backend.config import settings

# The pack's _load_token() reads (in order): SERIN_LICENSE_KEY env,
# SERIN_LICENSE_FILE, ~/.serin/license.key, then data/.serin-license. We own
# the last one — resolved next to the DB so it travels with the data dir.
_LICENSE_FILENAME = ".serin-license"


def license_path():
    return settings.db_path.parent / _LICENSE_FILENAME


def _looks_like_token(key: str) -> bool:
    """A Serin key is ``base64url(payload).base64url(sig)`` — one dot, two
    non-empty base64url parts. Core can't check the signature; it only rejects
    obvious garbage so a fat-fingered paste fails fast instead of silently."""
    if key.count(".") != 1:
        return False
    head, sig = key.split(".", 1)
    if not head or not sig:
        return False
    try:
        for part in (head, sig):
            base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    except (binascii.Error, ValueError):
        return False
    return True


def _peek_payload(key: str) -> dict:
    """Best-effort, UNVERIFIED decode of the token payload for display only.

    Never a source of truth — :func:`entitlements.summary` decides what is
    actually active. Used purely to show the user which email/plan a pasted
    key claims before the pack confirms it.
    """
    try:
        head = key.split(".", 1)[0]
        raw = base64.urlsafe_b64decode(head + "=" * (-len(head) % 4))
        data = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data.get(k) for k in ("email", "plan", "exp") if k in data}


def install_license(key: str) -> dict:
    """Validate the format and persist the key. Returns :func:`status`.

    Raises ``ValueError`` on a malformed key (caller maps to HTTP 400).
    """
    cleaned = (key or "").strip()
    if not cleaned:
        raise ValueError("License key is empty.")
    if not _looks_like_token(cleaned):
        raise ValueError("That doesn't look like a Serin license key (expected a signed token).")
    path = license_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cleaned, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass  # best-effort on filesystems without POSIX perms
    return status()


def clear_license() -> dict:
    """Remove the license file (revert to open-source). Idempotent."""
    path = license_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return status()


def install_pack(key: str) -> dict:
    """Redeem a license key for the Intelligence pack and install it locally.

    Downloads the tarball from the configured billing origin (the key is the
    credential), safely extracts it into the app-installed plugin dir, and
    saves the license. The pack is imported at startup, so this returns
    ``restart_required: True`` rather than attempting a hot import.

    Raises ``ValueError`` (→ 400) for a bad key / no billing URL, or
    ``RuntimeError`` (→ 502) when the download or extract fails.
    """
    import io
    import tarfile

    import httpx

    from backend import plugins

    cleaned = (key or "").strip()
    if not cleaned:
        # allow redeeming an already-saved key
        cleaned = license_path().read_text(encoding="utf-8").strip() if license_path().is_file() else ""
    if not _looks_like_token(cleaned):
        raise ValueError("A valid license key is required to download the pack.")
    if not settings.billing_url:
        raise ValueError("No billing URL configured (set SERIN_BILLING_URL).")

    url = settings.billing_url.rstrip("/") + "/pack/download"
    try:
        resp = httpx.get(url, headers={"authorization": f"Bearer {cleaned}"}, timeout=60, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not reach the pack server: {exc}") from exc
    if resp.status_code == 401:
        raise ValueError("The billing server rejected that license key.")
    if resp.status_code != 200:
        raise RuntimeError(f"Pack download failed ({resp.status_code}).")

    target = plugins.installed_pack_dir()
    target.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
            _safe_extract(tar, target)
    except (tarfile.TarError, OSError) as exc:
        raise RuntimeError(f"Pack archive could not be extracted: {exc}") from exc

    install_license(cleaned)
    return {"installed": True, "restart_required": True, "path": str(target)}


def _safe_extract(tar, dest) -> None:
    """Extract a tarball, refusing any member that escapes ``dest`` (path
    traversal / absolute paths / symlinks) — we control the producer, but
    never trust an archive blindly."""
    import os

    dest = dest.resolve()
    for member in tar.getmembers():
        member_path = (dest / member.name).resolve()
        if os.path.commonpath([str(dest), str(member_path)]) != str(dest):
            raise RuntimeError(f"unsafe path in archive: {member.name}")
        if member.issym() or member.islnk():
            raise RuntimeError(f"archive contains a link: {member.name}")
    tar.extractall(dest)  # noqa: S202 — members validated above


def status() -> dict:
    """Where the license stands: file present? env override? what's *active*?

    ``active`` reflects the pack verifier (the truth). ``installed`` is just
    "a file is on disk". They differ exactly when a key is saved but no pack
    is loaded — the case the UI must message honestly.
    """
    import os

    path = license_path()
    file_present = path.is_file()
    env_override = bool(os.environ.get("SERIN_LICENSE_KEY", "").strip())
    summary = entitlements.summary()
    claimed: dict = {}
    if file_present:
        try:
            claimed = _peek_payload(path.read_text(encoding="utf-8").strip())
        except OSError:
            claimed = {}
    return {
        "installed": file_present or env_override,
        "source": "env" if env_override else ("file" if file_present else "none"),
        "pack_loaded": entitlements._verifier is not None,
        "active": summary["plan"] != "opensource",
        "plan": summary["plan"],
        "features": summary["features"],
        "claimed": claimed,  # unverified; display-only
    }
