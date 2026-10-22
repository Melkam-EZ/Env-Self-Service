"""Static guardrails on the Terraform. terraform validate/plan need provider downloads; these run anywhere."""
import glob
import json
import os
import unittest

from envp.catalog import STANDARD_VARS, Catalog
from helpers import TEMPLATES

try:
    import hcl2
except ImportError:  # pragma: no cover
    hcl2 = None

REQUIRED_TAGS = {"managed-by", "env_id", "env_name", "team", "owner", "expires_at"}


def unq(x):
    return x.strip('"') if isinstance(x, str) else x


def parse(template):
    out = {"variable": {}, "resource": [], "provider": [], "text": ""}
    for path in sorted(glob.glob(os.path.join(TEMPLATES, template, "*.tf"))):
        with open(path) as f:
            text = f.read()
        out["text"] += "\n".join(l.split(" #")[0] for l in text.splitlines() if not l.strip().startswith("#")) + "\n"
        d = hcl2.loads(text)
        for v in d.get("variable", []):
            for name, body in v.items():
                out["variable"][unq(name)] = body
        for r in d.get("resource", []):
            for rtype, insts in r.items():
                for name, body in insts.items():
                    out["resource"].append((unq(rtype), unq(name), body))
        out["provider"] += d.get("provider", [])
    return out


@unittest.skipUnless(hcl2, "python-hcl2 not installed (pip install python-hcl2)")
class Guardrails(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = Catalog.load(TEMPLATES)
        cls.parsed = {n: parse(n) for n in cls.cat.templates}

    def aws(self):
        return [n for n, t in self.cat.templates.items() if t.provider == "aws"]

    def test_every_template_declares_the_standard_variables(self):
        for n, p in self.parsed.items():
            for v in STANDARD_VARS:
                self.assertIn(v, p["variable"], f"{n} must declare {v}")

    def test_catalog_params_match_terraform_variables(self):
        for n, t in self.cat.templates.items():
            for pname in t.params:
                self.assertIn(pname, self.parsed[n]["variable"], f"{n}: parameter {pname} has no variable")

    def test_every_variable_without_a_default_is_supplied_by_the_runner(self):
        # A required variable nobody sets would make every apply of this template fail.
        for n, p in self.parsed.items():
            supplied = set(STANDARD_VARS) | (set(["region"]) if self.cat.get(n).provider == "aws" else set())
            for name, body in p["variable"].items():
                if "default" not in body:
                    self.assertIn(name, supplied, f"{n}: variable {name} is required but never provided")

    def test_aws_templates_tag_everything_the_orphan_scan_needs(self):
        for n in self.aws():
            prov = [p for p in self.parsed[n]["provider"] if "aws" in {unq(k) for k in p}]
            self.assertTrue(prov, n)
            tags = {unq(k) for k in prov[0].get("aws", prov[0].get('"aws"'))["default_tags"][0]["tags"]}
            self.assertTrue(REQUIRED_TAGS <= tags, f"{n} is missing tags {REQUIRED_TAGS - tags}")

    def test_no_aws_template_opens_ingress_or_runs_provisioners(self):
        for n in self.aws():
            text = self.parsed[n]["text"]
            self.assertNotIn("ingress", text, n)
            self.assertNotIn("provisioner", text, n)
            self.assertNotIn("access_key", text, n)
            self.assertNotIn("secret_key", text, n)

    def test_s3_is_private_encrypted_and_always_deletable(self):
        res = {(t, n): b for t, n, b in self.parsed["s3-sandbox"]["resource"]}
        pab = res[("aws_s3_bucket_public_access_block", "this")]
        for key in ("block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets"):
            self.assertIs(pab[key], True, key)
        self.assertIn(("aws_s3_bucket_server_side_encryption_configuration", "this"), res)
        self.assertIs(res[("aws_s3_bucket", "this")]["force_destroy"], True)   # the reaper must never be blocked by a full bucket
        self.assertIn(("aws_s3_bucket_lifecycle_configuration", "this"), res)

    def test_ec2_is_hardened(self):
        res = {(t, n): b for t, n, b in self.parsed["ec2-dev"]["resource"]}
        box = res[("aws_instance", "box")]
        self.assertEqual(unq(box["metadata_options"][0]["http_tokens"]), "required")
        self.assertIs(box["root_block_device"][0]["encrypted"], True)
        self.assertNotIn("key_name", box)                                      # no SSH keys: SSM only

    def test_lambda_url_is_not_public(self):
        res = {(t, n): b for t, n, b in self.parsed["lambda-api"]["resource"]}
        self.assertEqual(unq(res[("aws_lambda_function_url", "fn")]["authorization_type"]), "AWS_IAM")
        self.assertEqual(res[("aws_cloudwatch_log_group", "fn")]["retention_in_days"], 1)

    def test_local_template_cleans_up_after_itself(self):
        provs = [b for t, n, b in self.parsed["local-demo"]["resource"] if t == "terraform_data"][0]["provisioner"]
        whens = [unq(next(iter(p.values())).get("when", "create")) for p in provs]
        self.assertIn("destroy", whens)

    def test_iam_is_never_wildcarded(self):
        for n in self.aws():
            self.assertNotRegex(self.parsed[n]["text"], r'actions\s*=\s*\["\*"\]', n)
            self.assertNotIn("AdministratorAccess", self.parsed[n]["text"])


if __name__ == "__main__":
    unittest.main()
