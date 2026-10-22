"""Runners turn an environment record into real infrastructure.

TerraformRunner shells out to terraform/tofu, one working directory (and one state) per environment.
FakeRunner records calls and can be told to fail, for tests and for trying the portal with no cloud.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time

from .util import iso


class RunnerError(Exception):
    pass


class TerraformRunner:
    def __init__(self, workroot, binary="terraform", timeout=1800, aws_region="eu-west-1",
                 state_bucket=None, state_lock_table=None, state_region=None, plugin_cache=None):
        self.workroot = workroot
        self.binary = binary
        self.timeout = timeout
        self.aws_region = aws_region
        self.state_bucket = state_bucket
        self.state_lock_table = state_lock_table
        self.state_region = state_region or aws_region
        self.plugin_cache = plugin_cache
        os.makedirs(workroot, exist_ok=True)
        if plugin_cache:
            os.makedirs(plugin_cache, exist_ok=True)

    def workdir(self, env_id: str) -> str:
        return os.path.join(self.workroot, env_id)

    # ---- public API used by the worker ------------------------------------------------------
    def provision(self, env, tmpl, log) -> dict:
        wd = self._prepare(env, tmpl)
        self._init(env, wd, log)
        self._run([self.binary, "apply", "-auto-approve", "-input=false", "-no-color",
                   "-var-file=envp.tfvars.json"], wd, log)
        return self._outputs(wd)

    def destroy(self, env, tmpl, log) -> None:
        wd = self.workdir(env["id"])
        if tmpl is None and not os.path.isdir(wd):
            log("nothing to destroy: no working directory and no template (already cleaned up)")
            return
        if tmpl is not None:
            wd = self._prepare(env, tmpl)
        self._init(env, wd, log)
        self._run([self.binary, "destroy", "-auto-approve", "-input=false", "-no-color",
                   "-var-file=envp.tfvars.json"], wd, log)
        if not self.state_bucket and self.state_resources(env["id"]) != 0:
            raise RunnerError("destroy finished but the state still lists resources")
        shutil.rmtree(wd, ignore_errors=True)

    # ---- helpers ----------------------------------------------------------------------------
    def _prepare(self, env, tmpl) -> str:
        wd = self.workdir(env["id"])
        os.makedirs(wd, exist_ok=True)
        for f in tmpl.tf_files():
            shutil.copy2(os.path.join(tmpl.path, f), os.path.join(wd, f))
        variables = {
            "env_id": env["id"],
            "env_name": env["name"],
            "team": env["team"],
            "owner": env["owner"],
            # Human-facing tag: when this environment is expected to be destroyed.
            "expires_at": iso(time.time() + env["ttl_minutes"] * 60),
        }
        variables.update(env["params"])
        if tmpl.provider == "aws":
            variables["region"] = self.aws_region
        # A JSON var file, never the command line: parameter values cannot inject shell or flags.
        with open(os.path.join(wd, "envp.tfvars.json"), "w") as f:
            json.dump(variables, f, indent=2)
        if self.state_bucket:
            with open(os.path.join(wd, "backend.tf"), "w") as f:
                f.write('terraform {\n  backend "s3" {}\n}\n')
        return wd

    def _init(self, env, wd, log):
        cmd = [self.binary, "init", "-input=false", "-no-color"]
        if self.state_bucket:
            cmd += [f"-backend-config=bucket={self.state_bucket}",
                    f"-backend-config=key=envp/{env['id']}/terraform.tfstate",
                    f"-backend-config=region={self.state_region}",
                    "-backend-config=encrypt=true"]
            if self.state_lock_table:
                cmd.append(f"-backend-config=dynamodb_table={self.state_lock_table}")
        self._run(cmd, wd, log)

    def _env(self):
        e = dict(os.environ, TF_IN_AUTOMATION="1", TF_INPUT="0", CHECKPOINT_DISABLE="1")
        e.setdefault("AWS_REGION", self.aws_region)
        if self.plugin_cache:
            e["TF_PLUGIN_CACHE_DIR"] = self.plugin_cache
        return e

    def _run(self, cmd, cwd, log):
        log("$ " + " ".join(cmd))
        p = subprocess.Popen(cmd, cwd=cwd, env=self._env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, start_new_session=True)
        lines = []

        def pump():
            for line in p.stdout:
                lines.append(line.rstrip())
                log(line.rstrip())

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        try:
            rc = p.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)  # the whole process group: terraform plus its providers/provisioners
            p.wait()
            t.join(5)
            p.stdout.close()
            raise RunnerError(f"{cmd[1]} timed out after {self.timeout}s")
        t.join(10)
        p.stdout.close()
        if rc != 0:
            raise RunnerError(f"{cmd[1]} exited {rc}: " + " | ".join(lines[-6:]))

    def _outputs(self, wd) -> dict:
        r = subprocess.run([self.binary, "output", "-json", "-no-color"], cwd=wd, env=self._env(),
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RunnerError("output failed: " + r.stderr.strip()[-300:])
        raw = json.loads(r.stdout or "{}")
        # Sensitive outputs never reach the database or the portal.
        return {k: v["value"] for k, v in raw.items() if not v.get("sensitive")}

    def state_resources(self, env_id: str):
        """Number of resources in the local state file; None if there is none."""
        p = os.path.join(self.workdir(env_id), "terraform.tfstate")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return len(json.load(f).get("resources", []))


class FakeRunner:
    def __init__(self, fail=None, delay=0.0, hook=None):
        self.live = {}              # env_id -> outputs (what "exists in the cloud")
        self.calls = []             # (kind, env_id)
        self.fail = dict(fail or {})  # {("provision"|"destroy", env_id or "*"): remaining failures}
        self.delay = delay
        self.hook = hook            # optional fn(kind, env) called inside the run (to simulate crashes)
        self._lock = threading.Lock()

    def _maybe_fail(self, kind, env_id):
        with self._lock:
            for key in ((kind, env_id), (kind, "*")):
                if self.fail.get(key, 0) > 0:
                    self.fail[key] -= 1
                    raise RunnerError(f"simulated {kind} failure")

    def provision(self, env, tmpl, log):
        with self._lock:
            self.calls.append(("provision", env["id"]))
        log(f"fake apply {env['id']}")
        if self.hook:
            self.hook("provision", env)
        if self.delay:
            time.sleep(self.delay)
        self._maybe_fail("provision", env["id"])
        out = {"url": f"https://{env['name']}.{env['team']}.example.test"}
        with self._lock:
            self.live[env["id"]] = out
        return out

    def destroy(self, env, tmpl, log):
        with self._lock:
            self.calls.append(("destroy", env["id"]))
        log(f"fake destroy {env['id']}")
        if self.hook:
            self.hook("destroy", env)
        if self.delay:
            time.sleep(self.delay)
        self._maybe_fail("destroy", env["id"])
        with self._lock:
            self.live.pop(env["id"], None)
