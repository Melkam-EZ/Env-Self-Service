"""Portal settings. Everything has a safe default; config/portal.json overrides."""
from __future__ import annotations

import json

DEFAULTS = {
    # Hard ceiling on one environment's total lifetime, whatever the template allows.
    "max_ttl_minutes": 1440,
    # A request whose estimated cost is above this needs a human approval.
    "approval_cost_threshold_usd": 5.0,
    # Approvals nobody answered are auto-rejected after this long.
    "approval_timeout_minutes": 1440,
    # Open environments (anything not destroyed/rejected/cancelled) per team.
    "max_active_per_team": 5,
    # Spend ceiling per team per calendar month. "default" applies to unlisted teams.
    "team_monthly_budget_usd": {"default": 50.0},
    # Tell the owner this many minutes before an environment is destroyed.
    "expiry_warning_minutes": 15,
    # Job retry policy (terraform apply/destroy are idempotent, so retrying is safe).
    "max_attempts": 3,
    "retry_base_seconds": 5,
    # A worker must heartbeat within this window or its job is given to someone else.
    "lease_seconds": 60,
    "workers": 2,
    # A destroy that failed is retried automatically after this long.
    "destroy_retry_minutes": 10,
    # Terraform runner timeout per command.
    "command_timeout_seconds": 1800,
    # Resources younger than this are not reported as orphans (cloud APIs lag).
    "orphan_grace_minutes": 30,
}


def load_config(path=None, overrides=None) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if path:
        with open(path) as f:
            user = json.load(f)
        _merge(cfg, user)
    if overrides:
        _merge(cfg, overrides)
    validate(cfg)
    return cfg


def _merge(cfg: dict, user: dict) -> None:
    for key, value in user.items():
        if key.startswith("_"):  # comments
            continue
        if key not in DEFAULTS:
            raise ValueError(f"unknown config key: {key}")
        cfg[key] = value


def validate(cfg: dict) -> None:
    budgets = cfg["team_monthly_budget_usd"]
    if not isinstance(budgets, dict) or "default" not in budgets:
        raise ValueError("team_monthly_budget_usd needs a 'default' entry")
    for key in ("max_ttl_minutes", "max_active_per_team", "max_attempts", "workers", "lease_seconds"):
        if not isinstance(cfg[key], int) or cfg[key] < 1:
            raise ValueError(f"{key} must be an integer >= 1")
    for key in ("approval_cost_threshold_usd", "retry_base_seconds"):
        if not isinstance(cfg[key], (int, float)) or cfg[key] < 0:
            raise ValueError(f"{key} must be a number >= 0")


def budget_for(cfg: dict, team: str) -> float:
    budgets = cfg["team_monthly_budget_usd"]
    return float(budgets.get(team, budgets["default"]))
