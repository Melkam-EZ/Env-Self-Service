"""SQLite state: environments, a job queue with leases, and an append-only audit log.

The store owns the state machine. Every status change is a compare-and-set
(UPDATE ... WHERE status IN (...)), so two actors (API, worker, reaper) can never
both win the same transition.

Environment states
  PENDING_APPROVAL -> QUEUED | REJECTED | CANCELLED
  QUEUED           -> PROVISIONING | CANCELLED
  PROVISIONING     -> ACTIVE | PROVISION_FAILED
  PROVISION_FAILED -> DESTROYING        (cleanup job is queued automatically)
  ACTIVE           -> DESTROY_QUEUED    (user, or the reaper when the TTL runs out)
  DESTROY_QUEUED   -> DESTROYING -> DESTROYED | DESTROY_FAILED
  DESTROY_FAILED   -> DESTROY_QUEUED    (retried automatically by the reaper)
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from .util import ApiError, new_env_id

OPEN = ("PENDING_APPROVAL", "QUEUED", "PROVISIONING", "ACTIVE", "PROVISION_FAILED",
        "DESTROY_QUEUED", "DESTROYING", "DESTROY_FAILED")
TERMINAL = ("DESTROYED", "REJECTED", "CANCELLED")
_IN_OPEN = ",".join(f"'{s}'" for s in OPEN)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS envs (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, team TEXT NOT NULL, owner TEXT NOT NULL,
  template TEXT NOT NULL, params TEXT NOT NULL, ttl_minutes INTEGER NOT NULL,
  status TEXT NOT NULL, hourly_usd REAL NOT NULL, cost_usd REAL NOT NULL,
  extensions INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL,
  approval_reasons TEXT NOT NULL DEFAULT '[]', approved_by TEXT, approved_at REAL,
  active_at REAL, expires_at REAL, destroyed_at REAL, outputs TEXT NOT NULL DEFAULT '{{}}',
  reason TEXT, warned INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS open_name ON envs(team, name) WHERE status IN ({_IN_OPEN});
CREATE INDEX IF NOT EXISTS envs_status ON envs(status);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, env_id TEXT NOT NULL, kind TEXT NOT NULL,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
  run_after REAL NOT NULL, lease_until REAL, worker TEXT, last_error TEXT,
  created_at REAL NOT NULL, finished_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_job ON jobs(env_id) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, env_id TEXT, actor TEXT,
  action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_env ON events(env_id);
"""

NOTIFY_ACTIONS = {"approval_requested", "ready", "expiry_warning", "destroyed", "provision_failed",
                  "destroy_failed", "rejected", "approved"}


def _env(row):
    if row is None:
        return None
    d = dict(row)
    d["params"] = json.loads(d["params"])
    d["outputs"] = json.loads(d["outputs"])
    d["approval_reasons"] = json.loads(d["approval_reasons"])
    return d


class Store:
    def __init__(self, path=":memory:", clock=time.time):
        self.clock = clock
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.listeners = []  # called as fn(action, env) after a transaction commits
        self._pending = []
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)

    # ---- plumbing -------------------------------------------------------------------------
    @contextmanager
    def tx(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            self._pending = []
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                self._pending = []
                raise
            pending, self._pending = self._pending, []
        for action, env_id in pending:  # outside the lock: listeners may do I/O
            if action in NOTIFY_ACTIONS:
                env = self.get_env(env_id)
                for fn in self.listeners:
                    try:
                        fn(action, env)
                    except Exception:  # a broken listener must never break state changes
                        pass

    def _event(self, db, env_id, actor, action, detail=""):
        db.execute("INSERT INTO events(ts, env_id, actor, action, detail) VALUES (?,?,?,?,?)",
                   (self.clock(), env_id, actor, action, detail))
        self._pending.append((action, env_id))

    def _cas(self, db, env_id, from_statuses, to, **fields) -> bool:
        sets = ["status=?", "updated_at=?"] + [f"{k}=?" for k in fields]
        marks = ",".join("?" for _ in from_statuses)
        vals = [to, self.clock()] + list(fields.values()) + [env_id] + list(from_statuses)
        cur = db.execute(f"UPDATE envs SET {', '.join(sets)} WHERE id=? AND status IN ({marks})", vals)
        return cur.rowcount == 1

    def _enqueue(self, db, env_id, kind, max_attempts, run_after=None) -> bool:
        try:
            db.execute(
                "INSERT INTO jobs(env_id, kind, status, max_attempts, run_after, created_at) VALUES (?,?,?,?,?,?)",
                (env_id, kind, "queued", max_attempts, run_after or self.clock(), self.clock()))
            return True
        except sqlite3.IntegrityError:  # a job for this env is already queued/running
            return False

    # ---- reads ----------------------------------------------------------------------------
    def get_env(self, env_id):
        with self.lock:
            return _env(self.db.execute("SELECT * FROM envs WHERE id=?", (env_id,)).fetchone())

    def list_envs(self, team=None, owner=None, statuses=None, limit=500):
        q, args = "SELECT * FROM envs WHERE 1=1", []
        if team:
            q += " AND team=?"
            args.append(team)
        if owner:
            q += " AND owner=?"
            args.append(owner)
        if statuses:
            q += f" AND status IN ({','.join('?' for _ in statuses)})"
            args += list(statuses)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            return [_env(r) for r in self.db.execute(q, args).fetchall()]

    def events_for(self, env_id, limit=200):
        with self.lock:
            rows = self.db.execute("SELECT * FROM events WHERE env_id=? ORDER BY id DESC LIMIT ?",
                                   (env_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def count_open(self, team) -> int:
        with self.lock:
            return self.db.execute(f"SELECT COUNT(*) FROM envs WHERE team=? AND status IN ({_IN_OPEN})",
                                   (team,)).fetchone()[0]

    def name_in_use(self, team, name) -> bool:
        with self.lock:
            return self.db.execute(f"SELECT 1 FROM envs WHERE team=? AND name=? AND status IN ({_IN_OPEN})",
                                   (team, name)).fetchone() is not None

    def team_spend(self, team, month_start) -> float:
        with self.lock:
            row = self.db.execute(
                "SELECT COALESCE(SUM(cost_usd),0) FROM envs WHERE team=? AND created_at>=? AND status!='REJECTED' "
                "AND status!='CANCELLED'", (team, month_start)).fetchone()
        return round(row[0], 4)

    def job_for(self, env_id):
        with self.lock:
            r = self.db.execute("SELECT * FROM jobs WHERE env_id=? ORDER BY id DESC LIMIT 1", (env_id,)).fetchone()
        return dict(r) if r else None

    def metrics(self) -> dict:
        with self.lock:
            by_status = dict(self.db.execute("SELECT status, COUNT(*) FROM envs GROUP BY status").fetchall())
            jobs = dict(self.db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
        return {"envs": by_status, "jobs": jobs}

    # ---- requests, approvals, cancellation ---------------------------------------------------
    def create_env(self, *, name, team, owner, template, params, ttl_minutes, hourly, cost, reasons,
                   max_attempts, max_open, budget_limit, month_start) -> dict:
        """Insert a request. Quota and budget are re-checked inside the transaction (races)."""
        env_id = new_env_id()
        now = self.clock()
        status = "PENDING_APPROVAL" if reasons else "QUEUED"
        with self.tx() as db:
            open_now = db.execute(f"SELECT COUNT(*) FROM envs WHERE team=? AND status IN ({_IN_OPEN})",
                                  (team,)).fetchone()[0]
            if open_now >= max_open:
                raise ApiError(409, f"team '{team}' already has {open_now} open environments (limit {max_open})")
            spent = db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM envs WHERE team=? AND created_at>=? "
                               "AND status NOT IN ('REJECTED','CANCELLED')", (team, month_start)).fetchone()[0]
            if spent + cost > budget_limit + 1e-9:
                raise ApiError(409, f"team '{team}' monthly budget exceeded (${spent:.2f} used + ${cost:.2f} > ${budget_limit:.2f})")
            try:
                db.execute(
                    "INSERT INTO envs(id,name,team,owner,template,params,ttl_minutes,status,hourly_usd,cost_usd,"
                    "created_at,updated_at,approval_reasons) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (env_id, name, team, owner, template, json.dumps(params, sort_keys=True), ttl_minutes, status,
                     hourly, cost, now, now, json.dumps(reasons)))
            except sqlite3.IntegrityError:
                raise ApiError(409, f"team '{team}' already has an open environment named '{name}'")
            self._event(db, env_id, owner, "requested", f"{template} ttl={ttl_minutes}m est=${cost:.4f}")
            if reasons:
                self._event(db, env_id, owner, "approval_requested", "; ".join(reasons))
            else:
                self._enqueue(db, env_id, "provision", max_attempts)
        return self.get_env(env_id)

    def approve(self, env_id, approver, max_attempts):
        with self.tx() as db:
            if not self._cas(db, env_id, ["PENDING_APPROVAL"], "QUEUED", approved_by=approver, approved_at=self.clock()):
                raise ApiError(409, "environment is not waiting for approval")
            self._enqueue(db, env_id, "provision", max_attempts)
            self._event(db, env_id, approver, "approved")
        return self.get_env(env_id)

    def reject(self, env_id, actor, reason, action="rejected"):
        with self.tx() as db:
            if not self._cas(db, env_id, ["PENDING_APPROVAL"], "REJECTED", reason=reason or "rejected"):
                raise ApiError(409, "environment is not waiting for approval")
            self._event(db, env_id, actor, action, reason or "")
        return self.get_env(env_id)

    def request_destroy(self, env_id, actor, reason, max_attempts):
        """Cancel (if never provisioned) or queue a destroy. Idempotent for in-flight destroys."""
        with self.tx() as db:
            env = _env(db.execute("SELECT * FROM envs WHERE id=?", (env_id,)).fetchone())
            if env is None:
                raise ApiError(404, "no such environment")
            st = env["status"]
            if st in ("PENDING_APPROVAL", "QUEUED"):
                self._cas(db, env_id, [st], "CANCELLED", reason=reason)
                db.execute("UPDATE jobs SET status='cancelled', finished_at=? WHERE env_id=? AND status='queued'",
                           (self.clock(), env_id))
                self._event(db, env_id, actor, "cancelled", reason)
            elif st in ("ACTIVE", "DESTROY_FAILED", "PROVISION_FAILED"):
                self._cas(db, env_id, [st], "DESTROY_QUEUED" if st != "PROVISION_FAILED" else st, reason=reason)
                self._enqueue(db, env_id, "destroy", max_attempts)
                self._event(db, env_id, actor, "destroy_requested", reason)
            elif st in ("DESTROY_QUEUED", "DESTROYING"):
                pass
            elif st == "PROVISIONING":
                raise ApiError(409, "environment is still being provisioned; try again when it is ACTIVE")
            else:
                raise ApiError(409, f"environment is already {st}")
        return self.get_env(env_id)

    def extend(self, env_id, actor, minutes, extra_cost, max_extensions):
        with self.tx() as db:
            env = _env(db.execute("SELECT * FROM envs WHERE id=?", (env_id,)).fetchone())
            if not env or env["status"] != "ACTIVE":
                raise ApiError(409, "only ACTIVE environments can be extended")
            if env["extensions"] >= max_extensions:
                raise ApiError(409, f"extension limit reached ({max_extensions})")
            db.execute("UPDATE envs SET expires_at=expires_at+?, extensions=extensions+1, cost_usd=cost_usd+?, "
                       "warned=0, updated_at=? WHERE id=?", (minutes * 60, extra_cost, self.clock(), env_id))
            self._event(db, env_id, actor, "extended", f"+{minutes}m")
        return self.get_env(env_id)

    # ---- job queue --------------------------------------------------------------------------
    def claim_job(self, worker, lease_seconds):
        """Atomically take the oldest due job and move its environment into the working state."""
        with self.tx() as db:
            while True:
                row = db.execute("SELECT * FROM jobs WHERE status='queued' AND run_after<=? ORDER BY id LIMIT 1",
                                 (self.clock(),)).fetchone()
                if row is None:
                    return None
                job = dict(row)
                if job["kind"] == "provision":
                    ok = self._cas(db, job["env_id"], ["QUEUED", "PROVISIONING"], "PROVISIONING")
                else:
                    ok = self._cas(db, job["env_id"],
                                   ["DESTROY_QUEUED", "PROVISION_FAILED", "DESTROY_FAILED", "DESTROYING"], "DESTROYING")
                if not ok:  # environment was cancelled/changed since the job was queued
                    db.execute("UPDATE jobs SET status='cancelled', finished_at=? WHERE id=?", (self.clock(), job["id"]))
                    continue
                db.execute("UPDATE jobs SET status='running', attempts=attempts+1, worker=?, lease_until=? WHERE id=?",
                           (worker, self.clock() + lease_seconds, job["id"]))
                self._event(db, job["env_id"], worker, f"{job['kind']}_started", f"attempt {job['attempts'] + 1}")
                job["attempts"] += 1
                job["worker"] = worker
                return job

    def heartbeat(self, job_id, worker, lease_seconds) -> bool:
        with self.tx() as db:
            cur = db.execute("UPDATE jobs SET lease_until=? WHERE id=? AND worker=? AND status='running'",
                             (self.clock() + lease_seconds, job_id, worker))
            return cur.rowcount == 1

    def job_done(self, job_id, worker, outputs=None) -> bool:
        """Fenced: only the worker that still owns the lease may record the result."""
        with self.tx() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=? AND worker=? AND status='running'",
                             (job_id, worker)).fetchone()
            if job is None:
                return False
            env = _env(db.execute("SELECT * FROM envs WHERE id=?", (job["env_id"],)).fetchone())
            now = self.clock()
            db.execute("UPDATE jobs SET status='done', finished_at=?, last_error=NULL WHERE id=?", (now, job_id))
            if job["kind"] == "provision":
                ttl = env["ttl_minutes"] * 60
                self._cas(db, env["id"], ["PROVISIONING"], "ACTIVE", active_at=now, expires_at=now + ttl,
                          outputs=json.dumps(outputs or {}), reason=None)
                self._event(db, env["id"], worker, "ready", "")
            else:
                actual = 0.0
                if env["active_at"]:
                    actual = round(env["hourly_usd"] * max(0.0, now - env["active_at"]) / 3600.0, 4)
                self._cas(db, env["id"], ["DESTROYING"], "DESTROYED", destroyed_at=now, cost_usd=actual)
                self._event(db, env["id"], worker, "destroyed", f"final cost ${actual:.4f}")
            return True

    def job_failed(self, job_id, worker, error, retry_base):
        """Return 'retry', 'final', or None if this worker no longer owns the job."""
        with self.tx() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=? AND worker=? AND status='running'",
                             (job_id, worker)).fetchone()
            if job is None:
                return None
            return self._failed(db, dict(job), error, retry_base, worker)

    def _failed(self, db, job, error, retry_base, actor):
        error = (error or "")[:2000]
        now = self.clock()
        if job["attempts"] < job["max_attempts"]:
            delay = min(900.0, retry_base * (2 ** (job["attempts"] - 1)))
            db.execute("UPDATE jobs SET status='queued', run_after=?, lease_until=NULL, worker=NULL, last_error=? WHERE id=?",
                       (now + delay, error, job["id"]))
            self._event(db, job["env_id"], actor, f"{job['kind']}_retry",
                        f"attempt {job['attempts']}/{job['max_attempts']} failed: {error[:200]}")
            return "retry"
        db.execute("UPDATE jobs SET status='failed', finished_at=?, last_error=? WHERE id=?", (now, error, job["id"]))
        if job["kind"] == "provision":
            self._cas(db, job["env_id"], ["PROVISIONING"], "PROVISION_FAILED", reason=f"provision failed: {error[:300]}")
            self._event(db, job["env_id"], actor, "provision_failed", error[:300])
            # Partial resources may exist. Queue a cleanup destroy right away.
            self._enqueue(db, job["env_id"], "destroy", job["max_attempts"])
        else:
            self._cas(db, job["env_id"], ["DESTROYING"], "DESTROY_FAILED", reason=f"destroy failed: {error[:300]}")
            self._event(db, job["env_id"], actor, "destroy_failed", error[:300])
        return "final"

    def requeue_stale(self, retry_base) -> int:
        """Give back jobs whose worker died (lease expired). Poison jobs run out of attempts."""
        n = 0
        with self.tx() as db:
            rows = db.execute("SELECT * FROM jobs WHERE status='running' AND lease_until<?", (self.clock(),)).fetchall()
            for r in rows:
                self._failed(db, dict(r), "worker lost its lease (crashed or hung)", 0, "reaper")
                n += 1
        return n

    # ---- reaper helpers ---------------------------------------------------------------------
    def expired_active(self):
        with self.lock:
            return [r["id"] for r in self.db.execute(
                "SELECT id FROM envs WHERE status='ACTIVE' AND expires_at<=?", (self.clock(),)).fetchall()]

    def warn_expiring(self, window_seconds) -> list:
        out = []
        with self.tx() as db:
            now = self.clock()
            rows = db.execute("SELECT id, expires_at FROM envs WHERE status='ACTIVE' AND warned=0 "
                              "AND expires_at>? AND expires_at<=?", (now, now + window_seconds)).fetchall()
            for r in rows:
                db.execute("UPDATE envs SET warned=1 WHERE id=?", (r["id"],))
                self._event(db, r["id"], "reaper", "expiry_warning", f"expires in {int((r['expires_at'] - now) // 60)}m")
                out.append(r["id"])
        return out

    def timeout_approvals(self, older_than_seconds) -> list:
        out = []
        with self.tx() as db:
            cutoff = self.clock() - older_than_seconds
            rows = db.execute("SELECT id FROM envs WHERE status='PENDING_APPROVAL' AND created_at<=?", (cutoff,)).fetchall()
            for r in rows:
                if self._cas(db, r["id"], ["PENDING_APPROVAL"], "REJECTED", reason="approval timed out"):
                    self._event(db, r["id"], "reaper", "rejected", "approval timed out")
                    out.append(r["id"])
        return out

    def retry_failed_destroys(self, older_than_seconds, max_attempts) -> list:
        out = []
        with self.tx() as db:
            cutoff = self.clock() - older_than_seconds
            rows = db.execute("SELECT id FROM envs WHERE status='DESTROY_FAILED' AND updated_at<=?", (cutoff,)).fetchall()
            for r in rows:
                if self._cas(db, r["id"], ["DESTROY_FAILED"], "DESTROY_QUEUED") and \
                        self._enqueue(db, r["id"], "destroy", max_attempts):
                    self._event(db, r["id"], "reaper", "destroy_retry", "automatic retry")
                    out.append(r["id"])
        return out

    def stuck_active(self, grace_seconds) -> list:
        """ACTIVE envs well past expiry: the reaper itself must be broken."""
        with self.lock:
            return [r["id"] for r in self.db.execute(
                "SELECT id FROM envs WHERE status='ACTIVE' AND expires_at<=?", (self.clock() - grace_seconds,)).fetchall()]
