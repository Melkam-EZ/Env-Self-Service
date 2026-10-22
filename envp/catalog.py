"""The template catalog: what developers may order, with typed parameters, pricing and approval rules.

Each template is a directory: templates/<name>/template.json + the Terraform files.
Developers never write Terraform; they fill in the parameters declared here.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

PARAM_TYPES = {"string", "integer", "boolean", "enum"}
PROVIDERS = {"aws", "local"}
# Terraform variables every template must declare (the runner always passes them).
STANDARD_VARS = ("env_id", "env_name", "team", "owner", "expires_at")
SAFE_STRING = r"^[A-Za-z0-9 ._-]*$"


class CatalogError(Exception):
    pass


@dataclass
class Param:
    name: str
    type: str
    description: str = ""
    default: object = None
    has_default: bool = False
    spec: dict = field(default_factory=dict)

    def check(self, value):
        """Return (clean_value, error_or_None)."""
        t, s = self.type, self.spec
        if t == "string":
            if not isinstance(value, str):
                return None, "must be a string"
            if len(value) > s.get("max_length", 64):
                return None, f"must be at most {s.get('max_length', 64)} characters"
            if not re.fullmatch(s.get("pattern", SAFE_STRING), value):
                return None, "contains characters that are not allowed"
            return value, None
        if t == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                return None, "must be a whole number"
            if "min" in s and value < s["min"]:
                return None, f"must be at least {s['min']}"
            if "max" in s and value > s["max"]:
                return None, f"must be at most {s['max']}"
            return value, None
        if t == "boolean":
            if not isinstance(value, bool):
                return None, "must be true or false"
            return value, None
        if t == "enum":
            if value not in s["values"]:
                return None, "must be one of: " + ", ".join(str(v) for v in s["values"])
            return value, None
        return None, "unknown type"

    def public(self) -> dict:
        d = {"name": self.name, "type": self.type, "description": self.description}
        if self.has_default:
            d["default"] = self.default
        d.update(self.spec)
        return d


@dataclass
class Template:
    name: str
    title: str
    description: str
    provider: str
    path: str
    params: dict
    min_ttl: int
    default_ttl: int
    max_ttl: int
    base_hourly: float
    rates: dict
    requires_approval: bool
    approval_rules: list
    max_extensions: int

    def validate_params(self, given: dict):
        """Return (clean_params, errors) with defaults applied and unknown keys refused."""
        errors, clean = {}, {}
        if not isinstance(given, dict):
            return {}, {"params": "must be an object"}
        for key in given:
            if key not in self.params:
                errors[key] = "unknown parameter"
        for name, p in self.params.items():
            if name in given:
                value, err = p.check(given[name])
                if err:
                    errors[name] = err
                else:
                    clean[name] = value
            elif p.has_default:
                clean[name] = p.default
            else:
                errors[name] = "is required"
        return clean, errors

    def hourly_cost(self, params: dict) -> float:
        total = self.base_hourly
        for name, spec in self.rates.items():
            value = params.get(name)
            p = self.params[name]
            if p.type == "enum":
                total += spec.get(str(value), 0.0)
            elif p.type == "integer":
                total += spec.get("per_unit", 0.0) * value
            elif p.type == "boolean":
                total += spec.get("true", 0.0) if value else spec.get("false", 0.0)
        return round(total, 6)

    def estimate(self, params: dict, ttl_minutes: int):
        hourly = self.hourly_cost(params)
        return hourly, round(hourly * ttl_minutes / 60.0, 4)

    def approval_reasons(self, params: dict) -> list:
        reasons = []
        if self.requires_approval:
            reasons.append(f"template '{self.name}' always needs approval")
        for rule in self.approval_rules:
            value = params.get(rule["param"])
            hit = ("equals" in rule and value == rule["equals"]) or ("in" in rule and value in rule["in"])
            if hit:
                reasons.append(rule["reason"])
        return reasons

    def public(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "provider": self.provider,
            "params": [p.public() for p in self.params.values()],
            "ttl": {"min": self.min_ttl, "default": self.default_ttl, "max": self.max_ttl},
            "base_hourly_usd": self.base_hourly,
            "requires_approval": self.requires_approval,
            "max_extensions": self.max_extensions,
        }

    def tf_files(self) -> list:
        return sorted(
            f for f in os.listdir(self.path) if f.endswith((".tf", ".tftpl")) and os.path.isfile(os.path.join(self.path, f))
        )


def _need(d: dict, key: str, typ, where: str):
    if key not in d or not isinstance(d[key], typ) or isinstance(d[key], bool) and typ is not bool:
        raise CatalogError(f"{where}: '{key}' missing or wrong type")
    return d[key]


def load_template(path: str) -> Template:
    name = os.path.basename(path.rstrip("/"))
    where = f"template {name}"
    try:
        with open(os.path.join(path, "template.json")) as f:
            raw = json.load(f)
    except (OSError, ValueError) as exc:
        raise CatalogError(f"{where}: cannot read template.json: {exc}")
    if raw.get("name") != name:
        raise CatalogError(f"{where}: 'name' must equal the directory name")
    if raw.get("provider") not in PROVIDERS:
        raise CatalogError(f"{where}: provider must be one of {sorted(PROVIDERS)}")
    ttl = _need(raw, "ttl", dict, where)
    mn, df, mx = (_need(ttl, k, int, where + " ttl") for k in ("min_minutes", "default_minutes", "max_minutes"))
    if not 1 <= mn <= df <= mx:
        raise CatalogError(f"{where}: need 1 <= min <= default <= max minutes")

    params = {}
    for pname, pspec in _need(raw, "params", dict, where).items():
        ptype = pspec.get("type")
        if ptype not in PARAM_TYPES:
            raise CatalogError(f"{where}: param {pname} has unknown type {ptype!r}")
        if ptype == "enum" and not pspec.get("values"):
            raise CatalogError(f"{where}: enum param {pname} needs 'values'")
        spec = {k: v for k, v in pspec.items() if k not in ("type", "description", "default")}
        p = Param(pname, ptype, pspec.get("description", ""), pspec.get("default"), "default" in pspec, spec)
        if p.has_default:
            _, err = p.check(p.default)
            if err:
                raise CatalogError(f"{where}: default of {pname} is invalid: {err}")
        params[pname] = p

    cost = _need(raw, "cost", dict, where)
    rates = cost.get("param_rates", {})
    for pname in rates:
        if pname not in params:
            raise CatalogError(f"{where}: cost rate for unknown param {pname}")
        if params[pname].type == "enum":
            missing = [str(v) for v in params[pname].spec["values"] if str(v) not in rates[pname]]
            if missing:
                raise CatalogError(f"{where}: no price for {pname} values {missing}")
        if params[pname].type == "string":
            raise CatalogError(f"{where}: cannot price a free-text parameter ({pname})")
    rules = raw.get("approval_rules", [])
    for r in rules:
        if r.get("param") not in params or ("equals" not in r and "in" not in r) or "reason" not in r:
            raise CatalogError(f"{where}: bad approval rule {r}")

    t = Template(
        name=name,
        title=_need(raw, "title", str, where),
        description=_need(raw, "description", str, where),
        provider=raw["provider"],
        path=path,
        params=params,
        min_ttl=mn,
        default_ttl=df,
        max_ttl=mx,
        base_hourly=float(cost.get("hourly_usd", 0.0)),
        rates=rates,
        requires_approval=bool(raw.get("requires_approval", False)),
        approval_rules=rules,
        max_extensions=int(raw.get("max_extensions", 2)),
    )
    if not any(f == "main.tf" for f in t.tf_files()):
        raise CatalogError(f"{where}: no main.tf")
    return t


class Catalog:
    def __init__(self, templates: dict):
        self.templates = templates

    @classmethod
    def load(cls, root: str) -> "Catalog":
        found = {}
        for entry in sorted(os.listdir(root)):
            p = os.path.join(root, entry)
            if os.path.isfile(os.path.join(p, "template.json")):
                found[entry] = load_template(p)
        if not found:
            raise CatalogError(f"no templates found in {root}")
        return cls(found)

    def get(self, name: str):
        return self.templates.get(name)

    def public(self) -> list:
        return [t.public() for t in self.templates.values()]
