"""External plugin loading — the open-core seam.

Modules in ``SERIN_PLUGINS_DIR`` are imported at startup. A plugin is a
regular Python file (or package) that uses the public connector SDK::

    # ~/serin-plugins/my_source.py
    from backend.connectors import ConnectorManifest, MarketDataConnector
    from backend.connectors.registry import register

    @register
    class MySource(MarketDataConnector): ...

This serves two audiences with one mechanism:

- **Community / private users** — ship out-of-tree connectors without
  forking (in-tree PRs remain the preferred path for shared connectors).
- **Serin Intelligence** — the proprietary pack is just a plugin package
  that additionally installs an entitlements verifier
  (see ``backend/entitlements.py`` and docs/BUSINESS-MODEL.md).

Failure posture: a broken plugin logs and is skipped; it must never take
down the core app.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

from backend.config import settings

logger = logging.getLogger(__name__)


def installed_pack_dir() -> Path:
    """Built-in fallback location for a pack installed via the app (see
    ``POST /api/admin/install-pack``): ``<data>/plugins`` beside the DB. Lets a
    subscriber activate Intelligence from the UI with no env var."""
    return settings.db_path.parent / "plugins"


def _plugin_dirs(plugins_dir: str | Path | None) -> list[Path]:
    """Directories to scan, in order: the explicit/env one first (it wins), then
    the app-installed pack dir. Deduplicated, existing dirs only."""
    dirs: list[Path] = []
    raw = plugins_dir if plugins_dir is not None else settings.plugins_dir
    if raw:
        dirs.append(Path(raw).expanduser())
    dirs.append(installed_pack_dir())
    seen: set[Path] = set()
    out: list[Path] = []
    for d in dirs:
        resolved = d.resolve() if d.exists() else d
        if resolved in seen:
            continue
        seen.add(resolved)
        if d.is_dir():
            out.append(d)
    return out


def _candidate_paths(plugins_dir: Path) -> list[Path]:
    """Loadable entries: top-level ``*.py`` files and packages."""
    candidates: list[Path] = []
    for entry in sorted(plugins_dir.iterdir()):
        if entry.name.startswith(("_", ".")):
            continue
        if entry.is_file() and entry.suffix == ".py":
            candidates.append(entry)
        elif entry.is_dir() and (entry / "__init__.py").exists():
            candidates.append(entry / "__init__.py")
    return candidates


def load_external_plugins(plugins_dir: str | Path | None = None) -> dict:
    """Import every plugin in the configured directory (and the app-installed
    pack dir as a fallback).

    Returns ``{"loaded": [names], "errors": {name: message}}`` — surfaced in
    logs and available to a future diagnostics endpoint.
    """
    result: dict = {"loaded": [], "errors": {}}
    paths: list[Path] = []
    for directory in _plugin_dirs(plugins_dir):
        paths.extend(_candidate_paths(directory))

    for path in paths:
        # my_source.py -> serin_plugin_my_source ; pack/__init__.py -> serin_plugin_pack
        base = path.parent.name if path.name == "__init__.py" else path.stem
        module_name = f"serin_plugin_{base}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"could not create import spec for {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            result["loaded"].append(base)
            logger.info("Loaded plugin %s from %s", base, path)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            result["errors"][base] = f"{type(exc).__name__}: {exc}"
            logger.exception("Plugin %s failed to load; continuing without it", base)
    return result
