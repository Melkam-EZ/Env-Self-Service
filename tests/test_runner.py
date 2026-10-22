import json
import os
import shutil
import stat
import tempfile
import time
import unittest

from envp.catalog import Catalog
from envp.runner import RunnerError, TerraformRunner
from helpers import TEMPLATES

SHIM = r"""#!/bin/sh
# Pretend terraform: records how it was called and keeps a tiny fake state file.
echo "$PWD|$*" >> "$SHIM_LOG"
case "$1" in
  init) exit 0 ;;
  apply)
    [ -n "$SHIM_FAIL" ] && { echo "Error: simulated apply failure"; echo "boom line 2"; exit 1; }
    [ -n "$SHIM_SLEEP" ] && sleep "$SHIM_SLEEP"
    echo '{"resources":[{"type":"x"}]}' > terraform.tfstate; echo "Apply complete" ;;
  destroy) echo '{"resources":[]}' > terraform.tfstate; echo "Destroy complete" ;;
  output) echo '{"url":{"value":"https://x.test"},"password":{"value":"hunter2","sensitive":true}}' ;;
esac
"""

ENV = {"id": "env-0a0b0c0d", "name": "web", "team": "payments", "owner": "alice", "ttl_minutes": 30,
       "params": {"greeting": "hi", "tier": "small"}}


class ShimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.shim = os.path.join(self.tmp, "terraform")
        with open(self.shim, "w") as f:
            f.write(SHIM)
        os.chmod(self.shim, os.stat(self.shim).st_mode | stat.S_IEXEC)
        self.log_file = os.path.join(self.tmp, "calls.log")
        os.environ["SHIM_LOG"] = self.log_file
        for k in ("SHIM_FAIL", "SHIM_SLEEP"):
            os.environ.pop(k, None)
        self.cat = Catalog.load(TEMPLATES)
        self.lines = []

    def tearDown(self):
        for k in ("SHIM_LOG", "SHIM_FAIL", "SHIM_SLEEP"):
            os.environ.pop(k, None)

    def calls(self):
        with open(self.log_file) as f:
            return [l.split("|", 1)[1].strip() for l in f]

    def runner(self, **kw):
        return TerraformRunner(os.path.join(self.tmp, "work"), binary=self.shim, **kw)

    def test_provision_runs_init_apply_output_in_order_with_a_vars_file(self):
        r = self.runner()
        out = r.provision(ENV, self.cat.get("local-demo"), self.lines.append)
        self.assertEqual(self.calls(), [
            "init -input=false -no-color",
            "apply -auto-approve -input=false -no-color -var-file=envp.tfvars.json",
            "output -json -no-color"])
        wd = r.workdir(ENV["id"])
        with open(os.path.join(wd, "envp.tfvars.json")) as f:
            v = json.load(f)
        self.assertEqual((v["env_id"], v["env_name"], v["team"], v["owner"]), (ENV["id"], "web", "payments", "alice"))
        self.assertEqual((v["greeting"], v["tier"]), ("hi", "small"))
        self.assertNotIn("region", v)                                 # local template: no region variable
        self.assertTrue(v["expires_at"].endswith("Z"))
        self.assertIn("main.tf", os.listdir(wd))
        self.assertNotIn("template.json", os.listdir(wd))             # catalog metadata is not Terraform
        self.assertEqual(out, {"url": "https://x.test"})              # sensitive output dropped
        self.assertTrue(any("Apply complete" in l for l in self.lines))

    def test_aws_templates_get_the_region(self):
        r = self.runner(aws_region="eu-west-2")
        r.provision(dict(ENV, params={"versioning": True}), self.cat.get("s3-sandbox"), lambda l: None)
        with open(os.path.join(r.workdir(ENV["id"]), "envp.tfvars.json")) as f:
            v = json.load(f)
        self.assertEqual((v["region"], v["versioning"]), ("eu-west-2", True))

    def test_values_are_data_not_command_line(self):
        r = self.runner()
        evil = dict(ENV, params={"greeting": "$(touch /tmp/pwned-envp); `id`", "tier": "small"})  # even if policy were bypassed
        r.provision(evil, self.cat.get("local-demo"), lambda l: None)
        self.assertFalse(any("pwned" in c for c in self.calls()))
        self.assertFalse(os.path.exists("/tmp/pwned-envp"))
        with open(os.path.join(r.workdir(ENV["id"]), "envp.tfvars.json")) as f:
            self.assertIn("touch", json.load(f)["greeting"])

    def test_remote_state_flags(self):
        r = self.runner(state_bucket="my-state", state_lock_table="locks", state_region="eu-west-1")
        r.provision(ENV, self.cat.get("local-demo"), lambda l: None)
        init = self.calls()[0]
        self.assertIn("-backend-config=bucket=my-state", init)
        self.assertIn(f"-backend-config=key=envp/{ENV['id']}/terraform.tfstate", init)   # one state file per environment
        self.assertIn("-backend-config=dynamodb_table=locks", init)
        self.assertIn("-backend-config=encrypt=true", init)
        with open(os.path.join(r.workdir(ENV["id"]), "backend.tf")) as f:
            self.assertIn('backend "s3"', f.read())

    def test_failure_carries_the_error_tail(self):
        os.environ["SHIM_FAIL"] = "1"
        with self.assertRaises(RunnerError) as cm:
            self.runner().provision(ENV, self.cat.get("local-demo"), self.lines.append)
        self.assertIn("simulated apply failure", str(cm.exception))
        self.assertIn("boom line 2", str(cm.exception))

    def test_timeout_kills_the_process(self):
        os.environ["SHIM_SLEEP"] = "30"
        t0 = time.time()
        with self.assertRaises(RunnerError) as cm:
            self.runner(timeout=1).provision(ENV, self.cat.get("local-demo"), lambda l: None)
        self.assertIn("timed out", str(cm.exception))
        self.assertLess(time.time() - t0, 10)

    def test_destroy_removes_workdir_only_when_state_is_empty(self):
        r = self.runner()
        r.provision(ENV, self.cat.get("local-demo"), lambda l: None)
        self.assertEqual(r.state_resources(ENV["id"]), 1)
        r.destroy(ENV, self.cat.get("local-demo"), lambda l: None)
        self.assertFalse(os.path.exists(r.workdir(ENV["id"])))
        self.assertTrue(any(c.startswith("destroy -auto-approve") for c in self.calls()))

    def test_destroy_is_idempotent_when_everything_is_already_gone(self):
        r = self.runner()
        r.destroy(ENV, None, self.lines.append)   # no workdir, no template: nothing to do, no error
        self.assertTrue(any("nothing to destroy" in l for l in self.lines))

    def test_destroy_refuses_to_claim_success_if_state_still_has_resources(self):
        r = self.runner()
        r.provision(ENV, self.cat.get("local-demo"), lambda l: None)
        with open(self.shim, "w") as f:      # a destroy that "succeeds" but leaves resources behind
            f.write(SHIM.replace('''destroy) echo '{"resources":[]}' > terraform.tfstate;''', 'destroy) true;'))
        with self.assertRaises(RunnerError) as cm:
            r.destroy(ENV, self.cat.get("local-demo"), lambda l: None)
        self.assertIn("still lists resources", str(cm.exception))
        self.assertTrue(os.path.exists(r.workdir(ENV["id"])))   # kept so a human can look


def real_binary():
    for name in (os.environ.get("ENVP_TF_BIN"), "terraform", "tofu"):
        if name and shutil.which(name):
            return shutil.which(name)
    return None


@unittest.skipUnless(real_binary(), "terraform/tofu not installed")
class RealTerraform(unittest.TestCase):
    """The local-demo template needs no cloud and no provider download, so this runs a REAL apply and destroy."""

    def test_real_apply_and_destroy(self):
        base = tempfile.mkdtemp()
        env = dict(ENV, id="env-1234abcd", params={"greeting": "real run", "tier": "small"})
        r = TerraformRunner(os.path.join(base, "work"), binary=real_binary(), timeout=120)
        out = r.provision(env, Catalog.load(TEMPLATES).get("local-demo"), lambda l: None)
        path = out["file"]
        try:
            with open(path) as f:
                text = f.read()
            self.assertIn("real run from web", text)
            self.assertEqual(r.state_resources(env["id"]), 1)
            r.provision(env, Catalog.load(TEMPLATES).get("local-demo"), lambda l: None)   # re-apply is safe (retries)
            r.destroy(env, Catalog.load(TEMPLATES).get("local-demo"), lambda l: None)
            self.assertFalse(os.path.exists(path))
            self.assertFalse(os.path.exists(r.workdir(env["id"])))
            r.destroy(env, None, lambda l: None)                                              # and so is destroying twice
        finally:
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main()
