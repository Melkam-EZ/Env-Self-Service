"""Orphan detection: resources that exist but no live environment owns them (they cost money silently).

Two sources:
  local  state directories on the portal host with no matching environment (or a destroyed one that still has state)
  aws    resources carrying our env_id tag whose environment is gone, via the Resource Groups Tagging API
"""
from __future__ import annotations

import json
import os
import subprocess

from .store import TERMINAL

TAG_MANAGED = "managed-by"
TAG_VALUE = "envportal"


def local_orphans(store, workroot) -> list:
    out = []
    if not os.path.isdir(workroot):
        return out
    for env_id in sorted(os.listdir(workroot)):
        wd = os.path.join(workroot, env_id)
        if not os.path.isdir(wd):
            continue
        env = store.get_env(env_id)
        resources = _state_count(wd)
        if env is None:
            out.append({"kind": "local", "env_id": env_id, "why": "working directory with no environment record",
                        "resources": resources})
        elif env["status"] in TERMINAL and resources:
            out.append({"kind": "local", "env_id": env_id, "why": f"environment is {env['status']} but state still lists resources",
                        "resources": resources})
    return out


def _state_count(wd):
    p = os.path.join(wd, "terraform.tfstate")
    try:
        with open(p) as f:
            return len(json.load(f).get("resources", []))
    except (OSError, ValueError):
        return 0


def aws_cli(args):
    r = subprocess.run(["aws"] + args, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:])
    return r.stdout


def aws_orphans(store, grace_seconds, cli=aws_cli) -> list:
    """Tagged AWS resources whose environment does not exist or is finished (past a grace period)."""
    raw = json.loads(cli(["resourcegroupstaggingapi", "get-resources", "--tag-filters",
                          f"Key={TAG_MANAGED},Values={TAG_VALUE}", "--output", "json"]))
    now = store.clock()
    out = []
    for item in raw.get("ResourceTagMappingList", []):
        tags = {t["Key"]: t["Value"] for t in item.get("Tags", [])}
        env_id = tags.get("env_id", "")
        env = store.get_env(env_id) if env_id else None
        if env is None:
            why = "tagged for an environment that does not exist"
        elif env["status"] in TERMINAL and (env["destroyed_at"] or 0) + grace_seconds < now:
            why = f"environment is {env['status']}"
        elif env["status"] == "DESTROY_FAILED":
            why = "environment destroy failed"
        else:
            continue
        out.append({"kind": "aws", "env_id": env_id, "arn": item["ResourceARN"], "why": why})
    return out
