#!/usr/bin/env python3
"""Encrypted backup of the shared Postgres database.

The per-tenant nightly job died with the per-tenant architecture, and the
pricing page promises nightly encrypted backups — so the shared deployment
needs its own. One database now holds *every* Cloud customer's portfolio,
which makes this both simpler and far more consequential than before.

    python -m scripts.backup_postgres                     # write one backup
    python -m scripts.backup_postgres --restore FILE      # decrypt to stdout
    python -m scripts.backup_postgres --verify FILE       # check it decrypts

Encrypted with AES-256-GCM under a key derived from ``BACKUP_MASTER_KEY``, so
the backup target never holds anything readable and a leaked object is useless
without the master key. GCM also authenticates: a truncated or tampered file
fails to decrypt rather than restoring silently corrupt data.

Self-hosters don't need this — SQLite is a file, and Connectors → Data already
does backup/restore.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AAD = b"serin-postgres-backup-v1"


def _key() -> bytes:
    master = os.environ.get("BACKUP_MASTER_KEY", "").strip()
    if not master:
        raise SystemExit(
            "BACKUP_MASTER_KEY is not set. Without it a backup would be stored "
            "in the clear — refusing rather than writing every customer's "
            "portfolio to disk unencrypted."
        )
    return hashlib.sha256(("serin-pg:" + master).encode()).digest()


def _database_url() -> str:
    """Connection for the dump — deliberately *not* the app's.

    The app connects as a role that row-level security applies to, which is the
    whole point of it. pg_dump under that role would silently emit only the
    rows visible to the current scope, so Postgres refuses outright:

        ERROR: query would be affected by row-level security policy for table …

    Backups therefore need the opposite role from the app: the table owner, or
    one with BYPASSRLS. Point BACKUP_DATABASE_URL at that. It is used for
    nothing else, and should never be the app's connection string.
    """
    url = (
        os.environ.get("BACKUP_DATABASE_URL", "").strip()
        or os.environ.get("SERIN_DATABASE_URL", "").strip()
    )
    if not url.startswith(("postgres://", "postgresql://")):
        raise SystemExit(
            "BACKUP_DATABASE_URL (or SERIN_DATABASE_URL) must point at Postgres. "
            "Self-hosted SQLite is a single file — copy it, or use "
            "Connectors → Data."
        )
    return url


def dump() -> bytes:
    """A consistent logical dump. pg_dump takes its own snapshot, so this is
    safe against a live database with users mid-request."""
    result = subprocess.run(
        ["pg_dump", "--no-owner", "--no-privileges", "--format=custom", _database_url()],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode()
        if "row-level security" in stderr:
            raise SystemExit(
                "pg_dump was blocked by row-level security, which means it is "
                "connecting as the application's role. That role only ever sees "
                "one user's rows, so the backup would be incomplete — Postgres "
                "refused rather than writing a partial dump.\n\n"
                "Set BACKUP_DATABASE_URL to a role that owns the tables or holds "
                "BYPASSRLS. It is the one place that legitimately needs to read "
                "every user's data.\n\n" + stderr[:300]
            )
        raise SystemExit(f"pg_dump failed: {stderr[:400]}")
    return result.stdout


def encrypt(blob: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(_key()).encrypt(nonce, blob, AAD)


def decrypt(blob: bytes) -> bytes:
    return AESGCM(_key()).decrypt(blob[:12], blob[12:], AAD)


def run_backup(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"serin-{stamp}.pgdump.enc"
    raw = dump()
    target.write_bytes(encrypt(raw))
    # Read it straight back. A backup nobody has decrypted is a hope, not a
    # backup — and this is the cheapest possible moment to find out.
    if decrypt(target.read_bytes()) != raw:
        target.unlink(missing_ok=True)
        raise SystemExit("backup failed verification immediately after writing")
    print(f"{target}  ({target.stat().st_size / 1_000_000:.1f} MB, verified)")
    return target


def prune(out_dir: Path, keep: int) -> None:
    backups = sorted(out_dir.glob("serin-*.pgdump.enc"))
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink()
        print(f"pruned {old.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=os.environ.get("BACKUP_DIR", "/data/backups"))
    parser.add_argument("--keep", type=int, default=int(os.environ.get("BACKUP_KEEP", "14")))
    parser.add_argument("--restore", metavar="FILE", help="decrypt to stdout (pipe to pg_restore)")
    parser.add_argument("--verify", metavar="FILE", help="check a backup decrypts")
    args = parser.parse_args()

    if args.restore:
        sys.stdout.buffer.write(decrypt(Path(args.restore).read_bytes()))
        return 0
    if args.verify:
        size = len(decrypt(Path(args.verify).read_bytes()))
        print(f"{args.verify}: decrypts cleanly, {size / 1_000_000:.1f} MB")
        return 0

    out_dir = Path(args.out)
    run_backup(out_dir)
    prune(out_dir, args.keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
