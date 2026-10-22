import contextlib
import hashlib
import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest

from envp import ctl
from envp.api import make_server
from envp.auth import Auth
from envp.runner import FakeRunner
from envp.server import App
from helpers import ROOT  # noqa: F401  (adds tests/ to path for imports)

TOKENS = {"alice": "t-alice", "dave": "t-dave", "bob": "t-bob", "carol": "t-carol", "admin": "t-admin"}
USERS = [
    ("alice", "payments", "developer"), ("dave", "payments", "developer"), ("bob", "search", "developer"),
    ("carol", "platform", "approver"), ("admin", "platform", "admin"),
]


def sha(t):
    return hashlib.sha256(t.encode()).hexdigest()


class ApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        auth = Auth([{"name": n, "team": t, "role": r, "token_sha256": sha(TOKENS[n])} for n, t, r in USERS])
        cls.runner = FakeRunner()
        cls.app = App(tempfile.mkdtemp(), runner=cls.runner, users=auth,
                      cfg=None, templates_dir=os.path.join(ROOT, "templates"))
        cls.app.start_background(reaper_interval=0.1, poll=0.05)
        cls.srv = make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.app.stop()

    # ---- helpers
    def call(self, method, path, who=None, body=None, raw=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if who:
            h["Authorization"] = "Bearer " + TOKENS[who]
        data = raw if raw is not None else (json.dumps(body) if body is not None else None)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        payload = r.read()
        c.close()
        try:
            return r.status, json.loads(payload), r
        except ValueError:
            return r.status, payload, r

    _counter = iter(range(1, 10**9))

    def name(self):
        return f"env{next(ApiBase._counter)}"

    def create(self, who="alice", **over):
        body = {"name": self.name(), "template": "local-demo", "params": {}, "ttl_minutes": 30}
        body.update(over)
        return self.call("POST", "/api/envs", who, body)

    def wait_for(self, env_id, status, who="alice", timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            s, d, _ = self.call("GET", f"/api/envs/{env_id}", who)
            if d.get("status") == status:
                return d
            time.sleep(0.05)
        self.fail(f"{env_id} never reached {status}; last={d.get('status')}")


class ApiTests(ApiBase):
    def test_authentication(self):
        self.assertEqual(self.call("GET", "/api/me")[0], 401)
        self.assertEqual(self.call("GET", "/api/me", headers={"Authorization": "Bearer nope"})[0], 401)
        s, d, _ = self.call("GET", "/api/me", "alice")
        self.assertEqual((s, d["name"], d["role"]), (200, "alice", "developer"))

    def test_open_endpoints_and_security_headers(self):
        self.assertEqual(self.call("GET", "/healthz")[:2], (200, {"ok": True}))
        s, body, r = self.call("GET", "/metrics")
        self.assertEqual(s, 200)
        self.assertIn(b"envp_environments", body)
        s, body, r = self.call("GET", "/")
        self.assertEqual(s, 200)
        self.assertIn(b"Environment Portal", body)
        self.assertIn("default-src 'self'", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(self.call("GET", "/app.js")[0], 200)
        s, _, r = self.call("GET", "/api/me", "alice")
        self.assertIn("frame-ancestors 'none'", r.getheader("Content-Security-Policy"))

    def test_portal_page_has_no_inline_script_or_style(self):
        with open(os.path.join(ROOT, "portal", "index.html")) as f:
            html = f.read()
        self.assertNotIn("<script>", html)
        self.assertNotIn("style=", html)
        with open(os.path.join(ROOT, "portal", "app.js")) as f:
            js = f.read()
        self.assertNotIn("innerHTML", js)   # user-controlled text is only ever inserted as text

    def test_routing_errors(self):
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("GET", "/../../etc/passwd")[0], 404)
        self.assertEqual(self.call("GET", "/portal/app.js")[0], 404)
        self.assertEqual(self.call("POST", "/api/me", "alice", {})[0], 405)
        self.assertEqual(self.call("POST", "/api/envs", "alice", raw="{not json")[0], 400)
        s, d, _ = self.call("POST", "/api/envs", "alice", raw="x" * 70000)
        self.assertEqual(s, 413)
        self.assertEqual(self.call("GET", "/api/envs/env-zzzz", "alice")[0], 404)

    def test_full_lifecycle_over_http(self):
        s, env, _ = self.create(params={"greeting": "hi"})
        self.assertEqual(s, 201)
        env = self.wait_for(env["id"], "ACTIVE")
        self.assertIn(env["id"], self.runner.live)
        self.assertEqual(env["outputs"]["url"].endswith(".payments.example.test"), True)
        self.assertGreater(env["expires_at_epoch"], time.time())
        s, d, _ = self.call("GET", f"/api/envs/{env['id']}/logs", "alice")
        self.assertTrue(any("done" in l for l in d["lines"]))
        s, d, _ = self.call("POST", f"/api/envs/{env['id']}/extend", "alice", {"minutes": 15})
        self.assertEqual((s, d["extensions"]), (200, 1))
        s, d, _ = self.call("POST", f"/api/envs/{env['id']}/destroy", "alice")
        self.assertEqual(s, 202)
        done = self.wait_for(env["id"], "DESTROYED")
        self.assertNotIn(env["id"], self.runner.live)
        actions = [e["action"] for e in done["events"]]
        self.assertEqual(actions[0], "requested")
        self.assertIn("destroyed", actions)

    def test_policy_refusals_explain_themselves(self):
        s, d, _ = self.create(ttl_minutes=99999, params={"tier": "huge"}, name="Bad Name")
        self.assertEqual(s, 422)
        self.assertEqual(set(d["details"]), {"name", "params.tier", "ttl_minutes"})

    def test_estimate_does_not_create_anything(self):
        before = len(self.call("GET", "/api/envs", "alice")[1]["environments"])
        s, d, _ = self.call("POST", "/api/estimate", "alice",
                            {"name": "preview-me", "template": "local-demo", "params": {"tier": "large"}, "ttl_minutes": 120})
        self.assertEqual((s, d["needs_approval"], d["cost_usd"]), (200, True, 8.0))
        self.assertEqual(len(self.call("GET", "/api/envs", "alice")[1]["environments"]), before)

    def test_templates_endpoint_exposes_a_form_schema(self):
        s, d, _ = self.call("GET", "/api/templates", "alice")
        t = {x["name"]: x for x in d["templates"]}["ec2-dev"]
        self.assertEqual({p["name"] for p in t["params"]}, {"instance_type", "volume_gb"})
        self.assertEqual(t["ttl"]["max"], 480)

    def test_approval_flow_over_http(self):
        s, env, _ = self.create(params={"tier": "large"}, ttl_minutes=120)
        self.assertEqual(env["status"], "PENDING_APPROVAL")
        self.assertEqual(self.call("POST", f"/api/envs/{env['id']}/approve", "alice")[0], 403)
        self.assertEqual(self.call("POST", f"/api/envs/{env['id']}/approve", "bob")[0], 404)  # other team can't even see it
        s, listed, _ = self.call("GET", "/api/envs?status=PENDING_APPROVAL", "carol")
        self.assertIn(env["id"], [e["id"] for e in listed["environments"]])
        s, d, _ = self.call("POST", f"/api/envs/{env['id']}/approve", "carol")
        self.assertEqual((s, d["approved_by"]), (200, "carol"))
        self.wait_for(env["id"], "ACTIVE")
        self.call("POST", f"/api/envs/{env['id']}/destroy", "alice")

    def test_reject_over_http(self):
        s, env, _ = self.create(params={"tier": "large"}, ttl_minutes=120)
        s, d, _ = self.call("POST", f"/api/envs/{env['id']}/reject", "carol", {"reason": "not this week"})
        self.assertEqual((s, d["status"], d["reason"]), (200, "REJECTED", "not this week"))

    def test_isolation_between_teams_and_roles(self):
        s, env, _ = self.create("alice")
        self.assertEqual(self.call("GET", f"/api/envs/{env['id']}", "bob")[0], 404)
        self.assertEqual(self.call("GET", f"/api/envs/{env['id']}", "dave")[0], 200)
        self.assertEqual(self.call("POST", f"/api/envs/{env['id']}/destroy", "dave")[0], 403)
        self.assertNotIn(env["id"], [e["id"] for e in self.call("GET", "/api/envs", "bob")[1]["environments"]])
        self.assertEqual(self.call("GET", "/api/orphans", "alice")[0], 403)
        self.assertEqual(self.call("GET", "/api/orphans", "admin")[1], {"orphans": []})
        self.assertEqual(self.call("POST", f"/api/envs/{env['id']}/destroy", "admin")[0], 202)

    def test_background_reaper_destroys_an_expired_environment(self):
        s, env, _ = self.create()
        env = self.wait_for(env["id"], "ACTIVE")
        # Pretend its time ran out 1 second ago, then let the real background reaper and worker do the rest.
        self.app.store.db.execute("UPDATE envs SET expires_at=? WHERE id=?", (time.time() - 1, env["id"]))
        done = self.wait_for(env["id"], "DESTROYED")
        self.assertNotIn(env["id"], self.runner.live)
        why = [e for e in done["events"] if e["action"] == "destroy_requested"][0]
        self.assertEqual(why["actor"], "reaper")

    def test_xss_payloads_never_get_stored(self):
        for payload in ("<script>alert(1)</script>", "x\"><img src=x onerror=alert(1)>"):
            s, d, _ = self.create(name=payload)
            self.assertEqual(s, 422)
            s, d, _ = self.create(params={"greeting": payload})
            self.assertEqual(s, 422)

    def test_metrics_reflect_state(self):
        self.assertEqual(self.create()[0], 201)  # make sure there is state to report
        _, text, _ = self.call("GET", "/metrics")
        self.assertIn(b'envp_environments{status="', text)
        self.assertIn(b"envp_team_spend_usd", text)


class CliTests(ApiBase):
    """The CLI talks to the same API; reuse the server."""

    def run_cli(self, who, *args):
        os.environ["ENVP_URL"] = f"http://127.0.0.1:{self.port}"
        os.environ["ENVP_TOKEN"] = TOKENS[who]
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = ctl.main(list(args))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_cli_round_trip(self):
        code, out, _ = self.run_cli("alice", "templates")
        self.assertIn("ec2-dev", out)
        code, out, _ = self.run_cli("alice", "request", "local-demo", "cli-one", "--ttl", "30", "--set", "greeting=hey")
        self.assertEqual(code, 0)
        env_id = out.split()[0]
        code, out, _ = self.run_cli("alice", "wait", env_id, "ACTIVE", "--timeout", "10")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("alice", "list", "--mine")
        self.assertIn(env_id, out)
        code, out, _ = self.run_cli("alice", "extend", env_id, "10")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("alice", "destroy", env_id)
        code, out, _ = self.run_cli("alice", "wait", env_id, "DESTROYED", "--timeout", "10")
        self.assertEqual(code, 0)

    def test_cli_dry_run_and_errors(self):
        code, out, _ = self.run_cli("alice", "request", "local-demo", "dry-run", "--ttl", "120", "--set", "tier=large", "--dry-run")
        self.assertTrue(json.loads(out)["needs_approval"])
        code, _, err = self.run_cli("alice", "request", "local-demo", "BAD NAME")
        self.assertEqual(code, 1)
        self.assertIn("name", err)
        code, _, err = self.run_cli("bob", "approve", "env-00000000")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
