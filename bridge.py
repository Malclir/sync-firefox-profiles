#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path(os.environ.get("BRIDGE_CONFIG", "/config/config.json"))
DEFAULT_COLLECTIONS = ("passwords", "bookmarks", "addresses", "creditcards", "forms", "history")


@dataclass(frozen=True)
class Account:
    name: str
    sessionfile: Path


@dataclass
class Record:
    id: str
    modified_unix: float
    payload: dict[str, Any]
    payload_json: str

    @property
    def deleted(self) -> bool:
        return bool(self.payload.get("deleted"))

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.payload).encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(
            f"Missing config: {path}. Copy config.example.json to this path first."
        )
    return json.loads(path.read_text())


def account_from_config(name: str, cfg: dict[str, Any]) -> Account:
    try:
        raw = cfg["accounts"][name]["sessionfile"]
    except KeyError as exc:
        raise SystemExit(f"Missing accounts.{name}.sessionfile in config") from exc
    return Account(name=name, sessionfile=Path(raw))


def save_config(path: Path, cfg: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, sort_keys=False) + "\n")


def run_ffs(
    cfg: dict[str, Any],
    account: Account,
    args: list[str],
    input_text: str | None = None,
) -> str:
    if not account.sessionfile.exists():
        raise RuntimeError(
            f"{account.name} session file does not exist: {account.sessionfile}"
        )

    cmd = [
        cfg.get("ffsclient_path", "ffsclient"),
        *args,
        "--sessionfile",
        str(account.sessionfile),
    ]
    result = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffsclient failed for {account.name}: {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout


def check_session(cfg: dict[str, Any], account: Account) -> None:
    run_ffs(cfg, account, ["check-session"])


def refresh_session(cfg: dict[str, Any], account: Account) -> None:
    run_ffs(cfg, account, ["refresh"])


def list_records(
    cfg: dict[str, Any], account: Account, collection: str
) -> dict[str, Record]:
    raw = run_ffs(cfg, account, ["list", collection, "--decoded", "--format", "json"])
    rows = json.loads(raw)
    records: dict[str, Record] = {}
    for row in rows:
        payload_raw = row.get("data", "{}")
        payload = (
            json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
        )
        if not isinstance(payload, dict):
            continue
        record_id = str(row["id"])
        records[record_id] = Record(
            id=record_id,
            modified_unix=float(row.get("modified_unix") or 0),
            payload=payload,
            payload_json=canonical_json(payload),
        )
    return records


def write_record(
    cfg: dict[str, Any], account: Account, collection: str, record: Record
) -> None:
    record_arg = f"--!arg={record.id}" if record.id.startswith("-") else record.id
    run_ffs(
        cfg,
        account,
        ["update", collection, record_arg, "--data-stdin", "--create"],
        input_text=record.payload_json,
    )


def choose_newer(left: Record, right: Record) -> Record:
    if left.modified_unix != right.modified_unix:
        return left if left.modified_unix > right.modified_unix else right

    return left if left.digest >= right.digest else right


def collection_plan(
    collection: str,
    source_name: str,
    target_name: str,
    source: dict[str, Record],
    target: dict[str, Record],
    sync_deletes: bool,
) -> list[tuple[str, Record, str]]:
    actions: list[tuple[str, Record, str]] = []

    for record_id, source_record in source.items():
        target_record = target.get(record_id)
        if target_record is None:
            if source_record.deleted and not sync_deletes:
                continue
            reason = f"{source_name} has {record_id}, {target_name} is missing it"
            actions.append((target_name, source_record, reason))
            continue

        if source_record.digest == target_record.digest:
            continue

        winner = choose_newer(source_record, target_record)
        if winner is not source_record:
            continue
        if winner.deleted and not sync_deletes:
            continue
        reason = f"{source_name} has newer/different {record_id} in {collection}"
        actions.append((target_name, source_record, reason))

    return actions


def sync_collection(
    cfg: dict[str, Any],
    source_account: Account,
    target_account: Account,
    collection: str,
    opts: dict[str, Any],
    max_writes: int | None,
    dry_run: bool,
) -> int:
    sync_deletes = bool(opts.get("sync_deletes", False))

    source_records = list_records(cfg, source_account, collection)
    target_records = list_records(cfg, target_account, collection)

    actions: list[tuple[Account, Record, str]] = []
    for target_name, record, reason in collection_plan(
        collection,
        source_account.name,
        target_account.name,
        source_records,
        target_records,
        sync_deletes,
    ):
        actions.append((target_account, record, reason))

    print(
        f"{source_account.name}->{target_account.name} {collection}: "
        f"source={len(source_records)} target={len(target_records)} actions={len(actions)} dry_run={dry_run}",
        flush=True,
    )

    planned = actions if max_writes is None else actions[:max_writes]
    if max_writes is not None and len(actions) > max_writes:
        print(
            f"  limiting this pass to {max_writes} of {len(actions)} actions",
            flush=True,
        )

    for target, record, reason in planned:
        status = "deleted tombstone" if record.deleted else "record"
        print(
            f"  {'would write' if dry_run else 'writing'} {status} {record.id} -> {target.name}: {reason}",
            flush=True,
        )
        if not dry_run:
            write_record(cfg, target, collection, record)

    return len(planned)


def enabled_route_collections(route: dict[str, Any], cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    selected = route.get("collections")
    global_collections = cfg.get("collections", {})
    collections: dict[str, dict[str, Any]] = {}

    for name in DEFAULT_COLLECTIONS:
        global_opts = dict(global_collections.get(name, {}))
        if selected is None:
            enabled = bool(global_opts.get("enabled", False))
        elif isinstance(selected, list):
            enabled = name in selected
        else:
            enabled = bool(selected.get(name, False))
        if enabled:
            global_opts["enabled"] = True
            collections[name] = global_opts
    return collections


def legacy_routes(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": "main-second",
            "source": "main",
            "target": "second",
            "bidirectional": True,
            "enabled": True,
            "dry_run": None,
            "collections": {
                name: bool(opts.get("enabled", False))
                for name, opts in cfg.get("collections", {}).items()
            },
        }
    ]


def run_once(
    cfg: dict[str, Any],
    only_collections: set[str] | None = None,
    max_writes: int | None = None,
) -> int:
    accounts = {name: account_from_config(name, cfg) for name in cfg.get("accounts", {})}

    for account in accounts.values():
        refresh_session(cfg, account)
        check_session(cfg, account)

    total = 0
    routes = cfg.get("routes") or legacy_routes(cfg)
    for route in routes:
        if not route.get("enabled", True):
            continue
        source = accounts[route["source"]]
        target = accounts[route["target"]]
        dry_run = cfg.get("dry_run", True) if route.get("dry_run") is None else bool(route.get("dry_run"))
        for collection, opts in enabled_route_collections(route, cfg).items():
            if only_collections is not None and collection not in only_collections:
                continue
            total += sync_collection(cfg, source, target, collection, opts, max_writes, dry_run)
            if route.get("bidirectional", False):
                total += sync_collection(cfg, target, source, collection, opts, max_writes, dry_run)
    return total


def run_loop(
    config_path: Path,
    cfg: dict[str, Any],
    only_collections: set[str] | None = None,
    max_writes: int | None = None,
) -> None:
    while True:
        try:
            cfg = load_config(config_path)
            total = run_once(cfg, only_collections, max_writes)
            print(f"sync pass complete: actions={total}", flush=True)
        except Exception as exc:
            print(f"sync pass failed: {exc}", file=sys.stderr, flush=True)
        interval = int(cfg.get("poll_seconds", 300))
        time.sleep(interval)


def write_default_config(path: Path) -> None:
    example = Path(__file__).with_name("config.example.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(example.read_text())
    print(f"Wrote {path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bridge selected Firefox Sync collections between two Mozilla accounts."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-config", help="Write example config to --config.")
    once_cmd = sub.add_parser("once", help="Run one sync pass.")
    once_cmd.add_argument(
        "--collection", action="append", help="Only sync this collection. Repeatable."
    )
    once_cmd.add_argument(
        "--max-writes", type=int, help="Limit writes/planned writes per collection."
    )
    loop_cmd = sub.add_parser("loop", help="Run forever.")
    loop_cmd.add_argument(
        "--collection", action="append", help="Only sync this collection. Repeatable."
    )
    loop_cmd.add_argument(
        "--max-writes",
        type=int,
        help="Limit writes/planned writes per collection per pass.",
    )
    args = parser.parse_args()

    if args.cmd == "init-config":
        write_default_config(args.config)
        return 0

    cfg = load_config(args.config)
    if args.cmd == "once":
        try:
            run_once(
                cfg, set(args.collection) if args.collection else None, args.max_writes
            )
            return 0
        except Exception as exc:
            print(f"sync pass failed: {exc}", file=sys.stderr)
            return 1
    if args.cmd == "loop":
        run_loop(
            args.config, cfg, set(args.collection) if args.collection else None, args.max_writes
        )
        return 0
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
