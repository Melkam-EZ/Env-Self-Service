"""envctl: command-line client for the portal API (same API the web UI uses).

  export ENVP_URL=http://127.0.0.1:8080 ENVP_TOKEN=<token>
  envctl templates | list | request | show | extend | destroy | approve | reject | logs | wait
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def call(method, path, body=None):
    url = os.environ.get("ENVP_URL", "http://127.0.0.1:8080") + path
    token = os.environ.get("ENVP_TOKEN", "")
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": e.reason}


def die(status, data):
    print(f"error {status}: {data.get('error')}", file=sys.stderr)
    if data.get("details"):
        for k, v in (data["details"].items() if isinstance(data["details"], dict) else [("", data["details"])]):
            print(f"  {k}: {v}", file=sys.stderr)
    sys.exit(1)


def ok(method, path, body=None):
    status, data = call(method, path, body)
    if status >= 400:
        die(status, data)
    return data


def row(e):
    left = ""
    if e["status"] == "ACTIVE" and e["expires_at_epoch"]:
        s = int(e["expires_at_epoch"] - time.time())
        left = "expires in " + (f"{s // 60}m{s % 60:02d}s" if s > 0 else "now")
    return f"{e['id']}  {e['status']:<16} {e['name']:<20} {e['template']:<12} ${e['cost_usd']:<8.4f} {left}"


def main(argv=None):
    p = argparse.ArgumentParser(prog="envctl")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("templates")
    l = sub.add_parser("list")
    l.add_argument("--mine", action="store_true")
    r = sub.add_parser("request")
    r.add_argument("template")
    r.add_argument("name")
    r.add_argument("--ttl", type=int, help="lifetime in minutes (template default if omitted)")
    r.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="template parameter (repeatable)")
    r.add_argument("--dry-run", action="store_true", help="only show the estimate and whether approval is needed")
    for name in ("show", "destroy", "approve", "logs"):
        sub.add_parser(name).add_argument("env_id")
    e = sub.add_parser("extend")
    e.add_argument("env_id")
    e.add_argument("minutes", type=int)
    j = sub.add_parser("reject")
    j.add_argument("env_id")
    j.add_argument("--reason", default="")
    w = sub.add_parser("wait")
    w.add_argument("env_id")
    w.add_argument("status", help="status to wait for, e.g. ACTIVE or DESTROYED")
    w.add_argument("--timeout", type=int, default=600)
    a = p.parse_args(argv)

    if a.cmd == "templates":
        for t in ok("GET", "/api/templates")["templates"]:
            print(f"{t['name']:<12} ttl {t['ttl']['min']}-{t['ttl']['max']}m (default {t['ttl']['default']})  {t['title']}")
            for prm in t["params"]:
                print(f"    {prm['name']:<14} {prm['type']:<8} default={prm.get('default')}  {prm['description']}")
    elif a.cmd == "list":
        envs = ok("GET", "/api/envs" + ("?mine=1" if a.mine else ""))["environments"]
        print("\n".join(row(x) for x in envs) or "(none)")
    elif a.cmd == "request":
        tmpl = next((t for t in ok("GET", "/api/templates")["templates"] if t["name"] == a.template), None)
        types = {x["name"]: x for x in (tmpl or {"params": []})["params"]}
        params = {}
        for kv in a.set:
            k, _, v = kv.partition("=")
            t = types.get(k, {}).get("type", "string")
            if t == "integer":
                v = int(v)
            elif t == "boolean":
                v = v.lower() in ("1", "true", "yes")
            elif t == "enum" and any(isinstance(x, int) for x in types[k]["values"]):
                v = int(v)
            params[k] = v
        body = {"name": a.name, "template": a.template, "params": params}
        if a.ttl:
            body["ttl_minutes"] = a.ttl
        if a.dry_run:
            print(json.dumps(ok("POST", "/api/estimate", body), indent=2))
        else:
            env = ok("POST", "/api/envs", body)
            print(row(env))
            if env["approval_reasons"]:
                print("needs approval: " + "; ".join(env["approval_reasons"]))
    elif a.cmd == "show":
        print(json.dumps(ok("GET", f"/api/envs/{a.env_id}"), indent=2))
    elif a.cmd == "logs":
        print("\n".join(ok("GET", f"/api/envs/{a.env_id}/logs")["lines"]))
    elif a.cmd in ("destroy", "approve"):
        print(row(ok("POST", f"/api/envs/{a.env_id}/{a.cmd}")))
    elif a.cmd == "reject":
        print(row(ok("POST", f"/api/envs/{a.env_id}/reject", {"reason": a.reason})))
    elif a.cmd == "extend":
        print(row(ok("POST", f"/api/envs/{a.env_id}/extend", {"minutes": a.minutes})))
    elif a.cmd == "wait":
        end = time.time() + a.timeout
        while time.time() < end:
            env = ok("GET", f"/api/envs/{a.env_id}")
            if env["status"] == a.status:
                print(row(env))
                return 0
            if env["status"] in ("REJECTED", "CANCELLED", "DESTROYED", "PROVISION_FAILED", "DESTROY_FAILED") and env["status"] != a.status:
                print(row(env))
                print(f"stopped in {env['status']}: {env.get('reason') or ''}", file=sys.stderr)
                return 1
            time.sleep(2)
        print("timed out", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
