"""Workers pull jobs from the queue and run them. They hold a lease and heartbeat it."""
from __future__ import annotations

import sys
import traceback
import os
import threading
import time
import uuid

from .util import iso


class LogStore:
    """One append-only log file per environment (what the portal shows as 'logs')."""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._lock = threading.Lock()

    def path(self, env_id):
        return os.path.join(self.root, f"{env_id}.log")

    def writer(self, env_id):
        def write(line):
            with self._lock, open(self.path(env_id), "a") as f:
                f.write(f"{iso(time.time())} {line}\n")
        return write

    def tail(self, env_id, n=200):
        try:
            with open(self.path(env_id)) as f:
                return f.read().splitlines()[-n:]
        except OSError:
            return []


class Worker:
    def __init__(self, store, runner, catalog, cfg, logs, worker_id=None):
        self.store, self.runner, self.catalog, self.cfg, self.logs = store, runner, catalog, cfg, logs
        self.id = worker_id or f"worker-{uuid.uuid4().hex[:6]}"

    def run_once(self) -> bool:
        lease = self.cfg["lease_seconds"]
        job = self.store.claim_job(self.id, lease)
        if job is None:
            return False
        env = self.store.get_env(job["env_id"])
        log = self.logs.writer(env["id"])
        log(f"== {job['kind']} attempt {job['attempts']}/{job['max_attempts']} by {self.id}")
        stop = threading.Event()
        beat = threading.Thread(target=self._heartbeat, args=(job["id"], lease, stop), daemon=True)
        beat.start()
        try:
            tmpl = self.catalog.get(env["template"])
            if job["kind"] == "provision":
                if tmpl is None:
                    raise RuntimeError(f"template '{env['template']}' is no longer in the catalog")
                outputs = self.runner.provision(env, tmpl, log)
            else:
                outputs = None
                self.runner.destroy(env, tmpl, log)
        except Exception as exc:  # noqa: BLE001 - any failure is a job failure
            stop.set()
            log(f"!! failed: {exc}")
            verdict = self.store.job_failed(job["id"], self.id, str(exc), self.cfg["retry_base_seconds"])
            log(f"== {verdict or 'lease lost, result discarded'}")
        else:
            stop.set()
            if self.store.job_done(job["id"], self.id, outputs):
                log("== done")
            else:
                log("== lease lost while running; result discarded (another worker will re-run it)")
        return True

    def _heartbeat(self, job_id, lease, stop):
        while not stop.wait(max(0.05, lease / 3.0)):
            if not self.store.heartbeat(job_id, self.id, lease):
                return

    def run_forever(self, stop: threading.Event, poll=1.0):
        while not stop.is_set():
            try:
                if not self.run_once():
                    stop.wait(poll)
            except Exception:  # noqa: BLE001 - keep the loop alive, but say why
                print(f"{self.id}: loop error:", file=sys.stderr)
                traceback.print_exc()
                stop.wait(poll)
