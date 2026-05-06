#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from flask import Flask, redirect, render_template, request, url_for

import bridge


CONFIG_PATH = Path(os.environ.get("BRIDGE_CONFIG", "/config/config.json"))
SESSION_DIR = Path(os.environ.get("SESSION_DIR", "/config/sessions"))
FFSCLIENT = os.environ.get("FFSCLIENT_PATH", "/usr/local/bin/ffsclient")
DEFAULT_COLLECTIONS = ("passwords", "bookmarks", "addresses", "creditcards", "forms", "history")

app = Flask(__name__)
login_procs: dict[str, subprocess.Popen[str]] = {}
login_lock = threading.Lock()
last_message = ""


def load_config() -> dict[str, Any]:
    return bridge.load_config(CONFIG_PATH)


def save_config(cfg: dict[str, Any]) -> None:
    bridge.save_config(CONFIG_PATH, cfg)


def truthy(value: str | None) -> bool:
    return value in {"1", "true", "on", "yes"}


def account_status(name: str, cfg: dict[str, Any]) -> str:
    session = Path(cfg.get("accounts", {}).get(name, {}).get("sessionfile", ""))
    proc = login_procs.get(name)
    if proc and proc.poll() is None:
        return "login running"
    if not session.exists():
        return "missing session"
    try:
        bridge.check_session(cfg, bridge.Account(name=name, sessionfile=session))
        return "ok"
    except Exception as exc:
        return f"error: {exc}".splitlines()[0]


def status_class(status: str) -> str:
    if status == "ok":
        return "ok"
    if "running" in status:
        return "warn"
    return "bad"


def login_account(name: str, email: str, password: str, otp: str) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    sessionfile = SESSION_DIR / f"{name}.secret"
    cmd = [
        FFSCLIENT,
        "login",
        email,
        password,
        "--sessionfile",
        str(sessionfile),
        "--device-name",
        f"firefox-sync-bridge-{name}",
        "--device-type",
        "server",
    ]
    if otp:
        cmd.extend(["--otp", otp])

    with login_lock:
        proc = login_procs.get(name)
        if proc and proc.poll() is None:
            raise RuntimeError(f"{name} login is already running")
        login_procs[name] = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )


def dashboard(message: str = ""):
    cfg = load_config()
    accounts = cfg.get("accounts", {})
    statuses = {name: account_status(name, cfg) for name in sorted(accounts)}
    return render_template(
        "dashboard.html",
        cfg=cfg,
        accounts=accounts,
        routes=cfg.get("routes", []),
        statuses=statuses,
        status_class=status_class,
        collections=DEFAULT_COLLECTIONS,
        message=message,
    )


@app.get("/")
def index():
    global last_message
    message = last_message
    last_message = ""
    return dashboard(message)


@app.post("/settings")
def settings():
    cfg = load_config()
    cfg["poll_seconds"] = max(10, int(request.form.get("poll_seconds", 300)))
    cfg["dry_run"] = truthy(request.form.get("dry_run"))
    save_config(cfg)
    set_message("Settings saved")
    return redirect(url_for("index"))


@app.post("/accounts")
def add_account():
    cfg = load_config()
    name = request.form["name"].strip()
    cfg.setdefault("accounts", {})[name] = {"sessionfile": f"/config/sessions/{name}.secret"}
    save_config(cfg)
    login_account(name, request.form["email"], request.form["password"], request.form.get("otp", ""))
    set_message(f"Started login for {name}")
    return redirect(url_for("index"))


@app.post("/accounts/remove")
def remove_account():
    cfg = load_config()
    cfg.setdefault("accounts", {}).pop(request.form["name"], None)
    save_config(cfg)
    set_message("Account removed from config")
    return redirect(url_for("index"))


@app.post("/routes/add")
def add_route():
    cfg = load_config()
    accounts = sorted(cfg.get("accounts", {}))
    if len(accounts) >= 2:
        cfg.setdefault("routes", []).append(
            {
                "name": f"{accounts[0]}-{accounts[1]}",
                "source": accounts[0],
                "target": accounts[1],
                "bidirectional": True,
                "enabled": True,
                "dry_run": None,
                "collections": {
                    name: name in ("passwords", "bookmarks", "addresses", "creditcards")
                    for name in DEFAULT_COLLECTIONS
                },
            }
        )
        save_config(cfg)
        set_message("Route added")
    return redirect(url_for("index"))


@app.post("/routes")
def save_routes():
    cfg = load_config()
    routes = cfg.get("routes", [])
    for idx, route in enumerate(routes):
        route["name"] = request.form.get(f"route_name_{idx}", route.get("name", ""))
        route["source"] = request.form.get(f"route_source_{idx}", route.get("source"))
        route["target"] = request.form.get(f"route_target_{idx}", route.get("target"))
        route["enabled"] = truthy(request.form.get(f"route_enabled_{idx}"))
        route["bidirectional"] = truthy(request.form.get(f"route_bidirectional_{idx}"))
        route["collections"] = {
            col: truthy(request.form.get(f"collection_{idx}_{col}"))
            for col in DEFAULT_COLLECTIONS
        }
    save_config(cfg)
    set_message("Routes saved")
    return redirect(url_for("index"))


@app.post("/run-once")
def run_once():
    threading.Thread(target=lambda: bridge.run_once(load_config()), daemon=True).start()
    set_message("Started one sync pass")
    return redirect(url_for("index"))


def set_message(message: str) -> None:
    global last_message
    last_message = message


def main() -> None:
    cfg = load_config()
    web = cfg.get("web", {})
    host = os.environ.get("WEB_HOST", web.get("host", "0.0.0.0"))
    port = int(os.environ.get("WEB_PORT", web.get("port", 8080)))
    print(f"portal listening on {host}:{port}", flush=True)
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
