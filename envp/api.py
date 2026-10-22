"""HTTP API + static portal, standard library only."""
from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .orphans import aws_orphans, local_orphans
from .util import ApiError, iso, month_start

MAX_BODY = 64 * 1024
PORTAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "portal")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "application/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'",
}


def public_env(env: dict, with_events=False) -> dict:
    out = {k: env[k] for k in ("id", "name", "team", "owner", "template", "params", "ttl_minutes", "status",
                               "hourly_usd", "cost_usd", "extensions", "approved_by", "outputs", "reason")}
    out["approval_reasons"] = env["approval_reasons"]
    for k in ("created_at", "approved_at", "active_at", "expires_at", "destroyed_at"):
        out[k] = iso(env[k])
        out[k + "_epoch"] = env[k]
    if with_events:
        out["events"] = [{"ts": iso(e["ts"]), "actor": e["actor"], "action": e["action"], "detail": e["detail"]}
                         for e in env.get("events", [])]
    return out


def metrics_text(app) -> str:
    m = app.store.metrics()
    lines = ["# TYPE envp_environments gauge"]
    lines += [f'envp_environments{{status="{k}"}} {v}' for k, v in sorted(m["envs"].items())]
    lines += ["# TYPE envp_jobs gauge"]
    lines += [f'envp_jobs{{status="{k}"}} {v}' for k, v in sorted(m["jobs"].items())]
    lines += ["# TYPE envp_team_spend_usd gauge"]
    teams = sorted({e["team"] for e in app.store.list_envs(limit=5000)})
    ms = month_start(app.store.clock())
    lines += [f'envp_team_spend_usd{{team="{t}"}} {app.store.team_spend(t, ms)}' for t in teams]
    return "\n".join(lines) + "\n"


class Routes:
    def __init__(self, app):
        self.app = app
        self.table = [
            ("GET", r"/api/me", self.me),
            ("GET", r"/api/templates", self.templates),
            ("POST", r"/api/estimate", self.estimate),
            ("GET", r"/api/envs", self.list_envs),
            ("POST", r"/api/envs", self.create),
            ("GET", r"/api/envs/(env-[0-9a-f]+)", self.get),
            ("POST", r"/api/envs/(env-[0-9a-f]+)/destroy", self.destroy),
            ("POST", r"/api/envs/(env-[0-9a-f]+)/extend", self.extend),
            ("POST", r"/api/envs/(env-[0-9a-f]+)/approve", self.approve),
            ("POST", r"/api/envs/(env-[0-9a-f]+)/reject", self.reject),
            ("GET", r"/api/envs/(env-[0-9a-f]+)/logs", self.logs),
            ("GET", r"/api/orphans", self.orphans),
        ]
        self.table = [(m, re.compile(p + "$"), fn) for m, p, fn in self.table]

    def match(self, method, path):
        allowed = False
        for m, rx, fn in self.table:
            mt = rx.match(path)
            if mt:
                if m == method:
                    return fn, mt.groups()
                allowed = True
        if allowed:
            raise ApiError(405, "method not allowed")
        raise ApiError(404, "not found")

    # handlers: fn(user, groups, body, query) -> (status, payload)
    def me(self, user, g, body, q):
        return 200, {"name": user["name"], "team": user["team"], "role": user["role"]}

    def templates(self, user, g, body, q):
        return 200, {"templates": self.app.catalog.public(), "limits": {
            "approval_cost_threshold_usd": self.app.cfg["approval_cost_threshold_usd"],
            "max_active_per_team": self.app.cfg["max_active_per_team"]}}

    def estimate(self, user, g, body, q):
        return 200, self.app.service.preview(user, body)

    def list_envs(self, user, g, body, q):
        envs = self.app.service.list(user, mine=q.get("mine") == "1", status=q.get("status"))
        return 200, {"environments": [public_env(e) for e in envs]}

    def create(self, user, g, body, q):
        env = self.app.service.create(user, body)
        return 201, public_env(env)

    def get(self, user, g, body, q):
        return 200, public_env(self.app.service.get(user, g[0]), with_events=True)

    def destroy(self, user, g, body, q):
        return 202, public_env(self.app.service.destroy(user, g[0]))

    def extend(self, user, g, body, q):
        return 200, public_env(self.app.service.extend(user, g[0], (body or {}).get("minutes")))

    def approve(self, user, g, body, q):
        return 200, public_env(self.app.service.approve(user, g[0]))

    def reject(self, user, g, body, q):
        return 200, public_env(self.app.service.reject(user, g[0], (body or {}).get("reason", "")))

    def logs(self, user, g, body, q):
        self.app.service.logs_allowed(user, g[0])
        return 200, {"lines": self.app.logs.tail(g[0], 300)}

    def orphans(self, user, g, body, q):
        if user["role"] != "admin":
            raise ApiError(403, "admin role required")
        found = local_orphans(self.app.store, self.app.workroot)
        if q.get("aws") == "1":
            try:
                found += aws_orphans(self.app.store, self.app.cfg["orphan_grace_minutes"] * 60)
            except Exception as exc:  # noqa: BLE001
                raise ApiError(502, f"AWS scan failed: {exc}")
        return 200, {"orphans": found}


def make_server(app, host="127.0.0.1", port=8080) -> ThreadingHTTPServer:
    routes = Routes(app)

    class Handler(BaseHTTPRequestHandler):
        server_version = "envp"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet by default; the audit log is in the database
            if os.environ.get("ENVP_ACCESS_LOG"):
                super().log_message(fmt, *args)

        def _send(self, status, payload, ctype="application/json", headers=None):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in {**SECURITY_HEADERS, **(headers or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method):
            url = urlparse(self.path)
            path = url.path
            try:
                # Always consume the body first so a keep-alive connection never desyncs.
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    self.close_connection = True
                    raise ApiError(413, "request body too large")
                raw = self.rfile.read(length) if length else b""
                if method == "GET" and path in STATIC:
                    fname, ctype = STATIC[path]
                    with open(os.path.join(PORTAL_DIR, fname), "rb") as f:
                        return self._send(200, f.read(), ctype)
                if method == "GET" and path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return
                if method == "GET" and path == "/healthz":
                    return self._send(200, {"ok": True})
                if method == "GET" and path == "/metrics":
                    return self._send(200, metrics_text(app).encode(), "text/plain; version=0.0.4")
                if not path.startswith("/api/"):
                    raise ApiError(404, "not found")

                fn, groups = routes.match(method, path)  # 404/405 before auth reveals nothing sensitive
                user = app.auth.authenticate(self.headers.get("Authorization"))
                if user is None:
                    raise ApiError(401, "missing or invalid token")
                body = None
                if raw:
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        raise ApiError(400, "body is not valid JSON")
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                status, payload = fn(user, groups, body, query)
                self._send(status, payload)
            except ApiError as e:
                self._send(e.status, {"error": e.message, "details": e.details})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": "internal error", "details": type(e).__name__})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv
