import json
import os
import shutil
import tempfile
import unittest

from envp import auth as authmod
from envp.catalog import Catalog, CatalogError, load_template
from envp.config import load_config
from helpers import ALICE, TEMPLATES, make


def ask(ctx, **body):
    body.setdefault("name", "web")
    body.setdefault("template", "local-demo")
    return ctx.service.preview(ALICE, body)


class Validation(unittest.TestCase):
    def setUp(self):
        self.ctx = make()

    def test_good_request(self):
        d = ask(self.ctx, params={"tier": "small"}, ttl_minutes=60)
        self.assertTrue(d["ok"])
        self.assertEqual((d["hourly_usd"], d["cost_usd"], d["needs_approval"]), (0.5, 0.5, False))
        self.assertEqual(d["params"], {"greeting": "hello", "tier": "small"})  # defaults filled in

    def test_names(self):
        for bad in ("A", "a", "UPPER", "has space", "-lead", "trail-", "x" * 21, "", None, 5, "dot.name", "semi;colon"):
            self.assertIn("name", ask(self.ctx, name=bad)["errors"], bad)
        for good in ("ab", "web-1", "x" * 20):
            self.assertNotIn("name", ask(self.ctx, name=good)["errors"], good)

    def test_parameters_are_typed_and_closed(self):
        cases = [
            ({"tier": "huge"}, "params.tier"),
            ({"tier": 1}, "params.tier"),
            ({"greeting": 5}, "params.greeting"),
            ({"greeting": "x" * 41}, "params.greeting"),
            ({"greeting": '"; rm -rf / #'}, "params.greeting"),     # shell/HCL metacharacters never get through
            ({"greeting": "${var.x}"}, "params.greeting"),
            ({"greeting": "a\nb"}, "params.greeting"),
            ({"surprise": 1}, "params.surprise"),                   # unknown keys are refused, not ignored
        ]
        for params, key in cases:
            self.assertIn(key, ask(self.ctx, params=params)["errors"], params)

    def test_integers_reject_bools_and_floats(self):
        for bad in (True, 7.5, "20", 7, 51):
            d = ask(self.ctx, template="ec2-dev", params={"volume_gb": bad})
            self.assertIn("params.volume_gb", d["errors"], bad)
        self.assertTrue(ask(self.ctx, template="ec2-dev", params={"volume_gb": 20})["ok"])

    def test_ttl_bounds_come_from_the_template_and_the_global_cap(self):
        self.assertIn("ttl_minutes", ask(self.ctx, ttl_minutes=0)["errors"])
        self.assertIn("ttl_minutes", ask(self.ctx, ttl_minutes=241)["errors"])
        self.assertIn("ttl_minutes", ask(self.ctx, ttl_minutes=True)["errors"])
        self.assertIn("ttl_minutes", ask(self.ctx, ttl_minutes="30")["errors"])
        self.assertTrue(ask(self.ctx, ttl_minutes=240)["ok"])
        capped = make({"max_ttl_minutes": 60})
        self.assertIn("ttl_minutes", ask(capped, ttl_minutes=61)["errors"])
        self.assertIn("ttl_minutes", ask(self.ctx, template="ec2-dev", ttl_minutes=10)["errors"])  # below min 30

    def test_unknown_template_and_bad_bodies(self):
        self.assertIn("template", ask(self.ctx, template="nope")["errors"])
        self.assertIn("body", self.ctx.service.preview(ALICE, ["x"])["errors"])
        self.assertIn("template", self.ctx.service.preview(ALICE, {"name": "ab", "template": {"a": 1}})["errors"])


class Costs(unittest.TestCase):
    def test_ec2_estimate_matches_the_rate_card(self):
        ctx = make()
        d = ask(ctx, template="ec2-dev", params={"instance_type": "t3.large", "volume_gb": 10}, ttl_minutes=240)
        self.assertAlmostEqual(d["hourly_usd"], 0.0832 + 10 * 0.00011, places=6)
        self.assertAlmostEqual(d["cost_usd"], d["hourly_usd"] * 4, places=3)

    def test_approval_rules_and_threshold(self):
        ctx = make()
        d = ask(ctx, template="ec2-dev", params={"instance_type": "t3.large"}, ttl_minutes=60)
        self.assertEqual(d["approval_reasons"], ["t3.large instances need approval"])
        self.assertFalse(ask(ctx, template="ec2-dev", params={"instance_type": "t3.small"}, ttl_minutes=60)["needs_approval"])
        big = ask(ctx, params={"tier": "large"}, ttl_minutes=120)   # $8
        self.assertTrue(big["needs_approval"] and "threshold" in big["approval_reasons"][0])
        edge = ask(ctx, params={"tier": "large"}, ttl_minutes=75)   # exactly $5.00: not above
        self.assertFalse(edge["needs_approval"])

    def test_preview_reports_the_budget(self):
        ctx = make({"team_monthly_budget_usd": {"default": 10, "payments": 20}})
        self.assertEqual(ask(ctx)["budget"]["limit_usd"], 20.0)


class CatalogLoading(unittest.TestCase):
    def test_shipped_templates_load(self):
        cat = Catalog.load(TEMPLATES)
        self.assertEqual(sorted(cat.templates), ["ec2-dev", "lambda-api", "local-demo", "s3-sandbox"])

    def copy(self, name="local-demo"):
        d = tempfile.mkdtemp()
        dst = os.path.join(d, name)
        shutil.copytree(os.path.join(TEMPLATES, name), dst)
        return dst

    def edit(self, path, fn):
        p = os.path.join(path, "template.json")
        with open(p) as f:
            data = json.load(f)
        fn(data)
        with open(p, "w") as f:
            json.dump(data, f)

    def test_broken_templates_are_refused_at_startup(self):
        cases = {
            "name must equal dir": lambda d: d.update(name="other"),
            "bad provider": lambda d: d.update(provider="gcp"),
            "ttl order": lambda d: d["ttl"].update(min_minutes=500),
            "bad default": lambda d: d["params"]["tier"].update(default="huge"),
            "missing price": lambda d: d["cost"]["param_rates"]["tier"].pop("large"),
            "priced free text": lambda d: d["cost"]["param_rates"].update(greeting={"x": 1}),
            "rate for unknown param": lambda d: d["cost"]["param_rates"].update(ghost={"x": 1}),
            "unknown type": lambda d: d["params"]["tier"].update(type="float"),
            "bad rule": lambda d: d.update(approval_rules=[{"param": "tier", "reason": "x"}]),
        }
        for label, mutate in cases.items():
            path = self.copy()
            self.edit(path, mutate)
            with self.assertRaises(CatalogError, msg=label):
                load_template(path)

    def test_template_without_terraform_is_refused(self):
        path = self.copy()
        os.remove(os.path.join(path, "main.tf"))
        with self.assertRaises(CatalogError):
            load_template(path)

    def test_empty_catalog_is_refused(self):
        with self.assertRaises(CatalogError):
            Catalog.load(tempfile.mkdtemp())


class Config(unittest.TestCase):
    def test_unknown_keys_and_missing_default_budget(self):
        with self.assertRaises(ValueError):
            load_config(overrides={"max_ttl": 1})
        with self.assertRaises(ValueError):
            load_config(overrides={"team_monthly_budget_usd": {"payments": 5}})
        with self.assertRaises(ValueError):
            load_config(overrides={"workers": 0})

    def test_shipped_config_is_valid(self):
        cfg = load_config(os.path.join(os.path.dirname(TEMPLATES), "config", "portal.json"))
        self.assertEqual(cfg["team_monthly_budget_usd"]["payments"], 100.0)


class Auth(unittest.TestCase):
    def test_tokens_are_stored_hashed_and_file_is_private(self):
        path = os.path.join(tempfile.mkdtemp(), "users.json")
        tokens = authmod.create_users_file(path)
        with open(path) as f:
            raw = f.read()
        for tok in tokens.values():
            self.assertNotIn(tok, raw)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        a = authmod.Auth.load(path)
        self.assertEqual(a.authenticate("Bearer " + tokens["carol"])["role"], "approver")
        for bad in (None, "", "Bearer ", "Bearer nope", tokens["alice"], "Basic " + tokens["alice"]):
            self.assertIsNone(a.authenticate(bad), bad)
        with self.assertRaises(FileExistsError):
            authmod.create_users_file(path)   # never silently replace existing tokens

    def test_unknown_role_is_refused(self):
        with self.assertRaises(ValueError):
            authmod.Auth([{"name": "x", "team": "t", "role": "root", "token_sha256": "0"}])


if __name__ == "__main__":
    unittest.main()
