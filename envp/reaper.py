"""The reaper is what makes environments ephemeral: it enforces every deadline."""
from __future__ import annotations

import sys
import threading
import traceback


class Reaper:
    def __init__(self, store, cfg):
        self.store, self.cfg = store, cfg

    def run_once(self) -> dict:
        s, c = self.store, self.cfg
        done = {}
        # 1. Workers that died mid-job: give the job back (or fail it if it keeps killing workers).
        done["recovered_jobs"] = s.requeue_stale(c["retry_base_seconds"])
        # 2. Time is up: queue the destroy.
        expired = []
        for env_id in s.expired_active():
            s.request_destroy(env_id, "reaper", "expired (TTL reached)", c["max_attempts"])
            expired.append(env_id)
        done["expired"] = expired
        # 3. Heads-up to owners before it happens.
        done["warned"] = s.warn_expiring(c["expiry_warning_minutes"] * 60)
        # 4. Nobody answered an approval request.
        done["approvals_timed_out"] = s.timeout_approvals(c["approval_timeout_minutes"] * 60)
        # 5. A destroy failed earlier: try again, forever. Cloud resources must not be forgotten.
        done["destroy_retried"] = s.retry_failed_destroys(c["destroy_retry_minutes"] * 60, c["max_attempts"])
        return done

    def run_forever(self, stop: threading.Event, interval=15.0):
        while not stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - keep reaping, but never fail silently
                print("reaper pass failed:", file=sys.stderr)
                traceback.print_exc()
            stop.wait(interval)
