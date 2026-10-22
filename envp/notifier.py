"""Optional webhook notifications (Slack-compatible: posts {"text": ...} plus structured fields)."""
from __future__ import annotations

import json
import threading
import urllib.request

MESSAGES = {
    "approval_requested": "needs approval",
    "approved": "was approved",
    "rejected": "was rejected",
    "ready": "is ready",
    "expiry_warning": "expires soon",
    "destroyed": "was destroyed",
    "provision_failed": "failed to provision (cleanup queued)",
    "destroy_failed": "could not be destroyed: needs a human",
}


class Notifier:
    def __init__(self, url=None, timeout=3.0):
        self.url, self.timeout = url, timeout
        self._threads = []

    def __call__(self, action, env):
        if not self.url or env is None:
            return
        text = f"[envp] {env['name']} ({env['team']}/{env['template']}) {MESSAGES.get(action, action)}"
        body = json.dumps({"text": text, "event": action, "env_id": env["id"], "status": env["status"],
                           "owner": env["owner"], "team": env["team"]}).encode()
        t = threading.Thread(target=self._post, args=(body,), daemon=True)
        t.start()
        self._threads.append(t)

    def _post(self, body):
        try:
            req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=self.timeout).read()
        except Exception:
            pass  # best effort: never block or break provisioning because chat is down

    def wait(self):
        for t in self._threads:
            t.join(5)
        self._threads = []
