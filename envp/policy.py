"""Guardrails. Pure functions: given a request, say yes/no, what it costs, and whether a human must approve."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import budget_for
from .util import month_start

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,18}[a-z0-9]$")


@dataclass
class Decision:
    ok: bool = False
    errors: dict = field(default_factory=dict)
    template: object = None
    params: dict = field(default_factory=dict)
    ttl_minutes: int = 0
    hourly_usd: float = 0.0
    cost_usd: float = 0.0
    reasons: list = field(default_factory=list)  # why a human must approve
    budget: dict = field(default_factory=dict)

    @property
    def needs_approval(self) -> bool:
        return bool(self.reasons)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "params": self.params,
            "ttl_minutes": self.ttl_minutes,
            "hourly_usd": self.hourly_usd,
            "cost_usd": self.cost_usd,
            "needs_approval": self.needs_approval,
            "approval_reasons": self.reasons,
            "budget": self.budget,
        }


def evaluate(body: dict, user: dict, catalog, cfg: dict, store, now: float) -> Decision:
    d = Decision()
    if not isinstance(body, dict):
        d.errors["body"] = "must be a JSON object"
        return d

    name = body.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        d.errors["name"] = "2-20 characters: lowercase letters, digits and '-', starting with a letter"
    elif store.name_in_use(user["team"], name):
        d.errors["name"] = f"your team already has an open environment called '{name}'"

    tmpl = catalog.get(body.get("template")) if isinstance(body.get("template"), str) else None
    if tmpl is None:
        d.errors["template"] = "unknown template"
        return d
    d.template = tmpl

    d.params, perrors = tmpl.validate_params(body.get("params", {}))
    for k, v in perrors.items():
        d.errors[f"params.{k}"] = v

    ttl = body.get("ttl_minutes", tmpl.default_ttl)
    ceiling = min(tmpl.max_ttl, cfg["max_ttl_minutes"])
    if isinstance(ttl, bool) or not isinstance(ttl, int):
        d.errors["ttl_minutes"] = "must be a whole number of minutes"
    elif not tmpl.min_ttl <= ttl <= ceiling:
        d.errors["ttl_minutes"] = f"must be between {tmpl.min_ttl} and {ceiling} minutes for this template"
    else:
        d.ttl_minutes = ttl

    if d.errors:
        return d

    d.hourly_usd, d.cost_usd = tmpl.estimate(d.params, d.ttl_minutes)

    open_now = store.count_open(user["team"])
    if open_now >= cfg["max_active_per_team"]:
        d.errors["quota"] = f"team '{user['team']}' already has {open_now} open environments (limit {cfg['max_active_per_team']})"

    limit = budget_for(cfg, user["team"])
    used = store.team_spend(user["team"], month_start(now))
    d.budget = {"used_usd": used, "limit_usd": limit, "remaining_usd": round(limit - used, 4)}
    if used + d.cost_usd > limit + 1e-9:
        d.errors["budget"] = (f"would exceed the monthly budget of team '{user['team']}' "
                              f"(${used:.2f} used + ${d.cost_usd:.2f} requested > ${limit:.2f})")

    d.reasons = tmpl.approval_reasons(d.params)
    if d.cost_usd > cfg["approval_cost_threshold_usd"]:
        d.reasons.append(f"estimated cost ${d.cost_usd:.2f} is above the ${cfg['approval_cost_threshold_usd']:.2f} approval threshold")

    d.ok = not d.errors
    return d


def evaluate_extension(env: dict, minutes, tmpl, cfg: dict, store, now: float):
    """Return (extra_cost, error). Extensions are budget-checked and cannot be used to dodge approval."""
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1:
        return 0.0, "minutes must be a positive whole number"
    if env["status"] != "ACTIVE":
        return 0.0, "only ACTIVE environments can be extended"
    if env["extensions"] >= tmpl.max_extensions:
        return 0.0, f"extension limit reached ({tmpl.max_extensions})"
    lifetime = (env["expires_at"] + minutes * 60 - env["active_at"]) / 60.0
    ceiling = min(tmpl.max_ttl, cfg["max_ttl_minutes"])
    if lifetime > ceiling + 1e-6:
        return 0.0, f"total lifetime would exceed the {ceiling}-minute limit"
    extra = round(env["hourly_usd"] * minutes / 60.0, 4)
    total = env["cost_usd"] + extra
    if env["approved_by"] is None and total > cfg["approval_cost_threshold_usd"]:
        return 0.0, ("this extension would push the environment above the approval threshold; "
                     "request a new environment so it can be approved")
    limit = budget_for(cfg, env["team"])
    used = store.team_spend(env["team"], month_start(now))
    if used + extra > limit + 1e-9:
        return 0.0, f"would exceed the monthly budget of team '{env['team']}'"
    return extra, None
