"""Wires everything together and provides the `python3 -m envp` commands."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time

from . import auth as authmod
from .api import make_server
from .catalog import Catalog
from .config import load_config
from .notifier import Notifier
from .orphans import aws_orphans, local_orphans
from .reaper import Reaper
from .runner import FakeRunner, TerraformRunner
from .service import Service
from .store import Store
from .worker import LogStore, Worker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class App:
    def __init__(self, data_dir, config_path=None, templates_dir=None, runner=None, clock=time.time,
                 webhook_url=None, users=None, cfg=None):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.cfg = cfg or load_config(config_path)
        self.catalog = Catalog.load(templates_dir or os.path.join(ROOT, "templates"))
        self.store = Store(os.path.join(data_dir, "envp.db"), clock=clock)
        self.workroot = os.path.join(data_dir, "work")
        self.logs = LogStore(os.path.join(data_dir, "logs"))
        self.runner = runner or build_runner(self.cfg, self.workroot, data_dir)
        self.service = Service(self.store, self.catalog, self.cfg)
        self.auth = users or authmod.Auth.load(os.path.join(data_dir, "users.json"))
        self.notifier = Notifier(webhook_url)
        self.store.listeners.append(self.notifier)
        self.workers = [Worker(self.store, self.runner, self.catalog, self.cfg, self.logs, f"worker-{i + 1}")
                        for i in range(self.cfg["workers"])]
        self.reaper = Reaper(self.store, self.cfg)
        self._stop = threading.Event()
        self._threads = []

    def start_background(self, reaper_interval=15.0, poll=1.0):
        for w in self.workers:
            t = threading.Thread(target=w.run_forever, args=(self._stop, poll), daemon=True, name=w.id)
            t.start()
            self._threads.append(t)
        t = threading.Thread(target=self.reaper.run_forever, args=(self._stop, reaper_interval), daemon=True, name="reaper")
        t.start()
        self._threads.append(t)

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(5)


def build_runner(cfg, workroot, data_dir):
    kind = os.environ.get("ENVP_RUNNER", "terraform")
    if kind == "fake":
        return FakeRunner()
    return TerraformRunner(
        workroot,
        binary=os.environ.get("ENVP_TF_BIN", "terraform"),
        timeout=cfg["command_timeout_seconds"],
        aws_region=os.environ.get("ENVP_AWS_REGION", "eu-west-1"),
        state_bucket=os.environ.get("ENVP_STATE_BUCKET") or None,
        state_lock_table=os.environ.get("ENVP_STATE_LOCK_TABLE") or None,
        plugin_cache=os.path.join(data_dir, "plugin-cache"),
    )


def _app_from_env(args) -> App:
    return App(args.data, config_path=args.config, templates_dir=args.templates,
               webhook_url=os.environ.get("ENVP_WEBHOOK_URL") or None)


def cmd_init(args):
    os.makedirs(args.data, exist_ok=True)
    path = os.path.join(args.data, "users.json")
    if os.path.exists(path):
        print(f"{path} already exists; delete it to create new tokens", file=sys.stderr)
        return 1
    tokens = authmod.create_users_file(path)
    print("Tokens (shown once, stored only as hashes):")
    for name, tok in tokens.items():
        print(f"  {name:<6} {tok}")
    return 0


def cmd_serve(args):
    app = _app_from_env(args)
    app.start_background(reaper_interval=float(os.environ.get("ENVP_REAPER_INTERVAL", "15")))
    srv = make_server(app, args.bind, args.port)
    stop = threading.Event()

    def shutdown(*_):
        stop.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"envp listening on http://{args.bind}:{args.port} "
          f"(runner={type(app.runner).__name__}, templates={','.join(app.catalog.templates)})", flush=True)
    srv.serve_forever()
    app.stop()
    return 0


def cmd_reap_once(args):
    app = _app_from_env(args)
    print(json.dumps(app.reaper.run_once(), indent=2))
    return 0


def cmd_orphans(args):
    app = _app_from_env(args)
    found = local_orphans(app.store, app.workroot)
    if args.aws:
        found += aws_orphans(app.store, app.cfg["orphan_grace_minutes"] * 60)
    print(json.dumps(found, indent=2))
    return 1 if found else 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="envp")
    p.add_argument("--data", default=os.environ.get("ENVP_DATA", os.path.join(ROOT, "data")))
    p.add_argument("--config", default=os.environ.get("ENVP_CONFIG", os.path.join(ROOT, "config", "portal.json")))
    p.add_argument("--templates", default=os.environ.get("ENVP_TEMPLATES", os.path.join(ROOT, "templates")))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create users and tokens").set_defaults(fn=cmd_init)
    s = sub.add_parser("serve", help="run portal, API, workers and reaper")
    s.add_argument("--bind", default=os.environ.get("ENVP_BIND", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("ENVP_PORT", "8080")))
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("reap-once", help="run one reaper pass").set_defaults(fn=cmd_reap_once)
    o = sub.add_parser("orphans", help="list resources nothing owns (exit 1 if any)")
    o.add_argument("--aws", action="store_true", help="also scan AWS by tag")
    o.set_defaults(fn=cmd_orphans)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
