#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


CONFIG_PATH = Path(os.environ.get("BRIDGE_CONFIG", "/config/config.json"))
EXAMPLE_CONFIG = Path("/app/config.example.json")
SESSION_DIR = Path(os.environ.get("SESSION_DIR", "/config/sessions"))
DEFAULT_COLLECTIONS = ("passwords", "bookmarks", "addresses", "creditcards", "forms", "history")


def bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def ensure_config() -> dict[str, Any]:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
    else:
        cfg = json.loads(EXAMPLE_CONFIG.read_text())

    default_cfg = json.loads(EXAMPLE_CONFIG.read_text())

    cfg["dry_run"] = bool_env("BRIDGE_DRY_RUN", cfg.get("dry_run", True))
    cfg["poll_seconds"] = int_env("POLL_SECONDS", cfg.get("poll_seconds", 300))
    cfg.setdefault("web", default_cfg.get("web", {}))

    collections = cfg.setdefault("collections", {})
    for name in DEFAULT_COLLECTIONS:
        opts = collections.setdefault(name, {})
        opts["enabled"] = bool_env(f"ENABLE_{name.upper()}", opts.get("enabled", False))
        opts["sync_deletes"] = bool_env(f"SYNC_DELETES_{name.upper()}", opts.get("sync_deletes", False))

    if "routes" not in cfg:
        cfg["routes"] = default_cfg.get("routes", [])

    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, sort_keys=False) + "\n")
    return cfg


def bootstrap() -> None:
    ensure_config()


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare Firefox Sync bridge config and sessions.")
    parser.add_argument("command", choices=["run"])
    args = parser.parse_args()
    if args.command == "run":
        bootstrap()
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
