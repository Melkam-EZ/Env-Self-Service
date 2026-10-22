"""Use cases and permissions. The API is a thin layer over this."""
from __future__ import annotations

from . import policy
from .config import budget_for
from .util import ApiError, month_start

ROLES = ("developer", "approver", "admin")


class Service:
    def __init__(self, store, catalog, cfg):
        self.store, self.catalog, self.cfg = store, catalog, cfg

    # ---- permissions ------------------------------------------------------------------------
    @staticmethod
    def can_view(user, env) -> bool:
        return user["role"] in ("approver", "admin") or env["team"] == user["team"]

    def _load(self, user, env_id, *, write=False):
        env = self.store.get_env(env_id)
        # 404 (not 403) for other teams' environments: do not leak that they exist.
        if env is None or not self.can_view(user, env):
            raise ApiError(404, "no such environment")
        if write and not (user["role"] == "admin" or env["owner"] == user["name"]):
            raise ApiError(403, "only the owner or an admin can do that")
        return env

    # ---- use cases --------------------------------------------------------------------------
    def preview(self, user, body) -> dict:
        d = policy.evaluate(body, user, self.catalog, self.cfg, self.store, self.store.clock())
        return d.to_dict()

    def create(self, user, body) -> dict:
        d = policy.evaluate(body, user, self.catalog, self.cfg, self.store, self.store.clock())
        if not d.ok:
            raise ApiError(422, "request refused by policy", d.errors)
        return self.store.create_env(
            name=body["name"], team=user["team"], owner=user["name"], template=d.template.name, params=d.params,
            ttl_minutes=d.ttl_minutes, hourly=d.hourly_usd, cost=d.cost_usd, reasons=d.reasons,
            max_attempts=self.cfg["max_attempts"], max_open=self.cfg["max_active_per_team"],
            budget_limit=budget_for(self.cfg, user["team"]), month_start=month_start(self.store.clock()))

    def get(self, user, env_id) -> dict:
        env = self._load(user, env_id)
        env["events"] = self.store.events_for(env_id)
        return env

    def list(self, user, mine=False, status=None) -> list:
        statuses = [s for s in (status or "").split(",") if s] or None
        if user["role"] in ("approver", "admin") and not mine:
            return self.store.list_envs(statuses=statuses)
        if mine:
            return self.store.list_envs(owner=user["name"], statuses=statuses)
        return self.store.list_envs(team=user["team"], statuses=statuses)

    def approve(self, user, env_id) -> dict:
        env = self._load(user, env_id)
        self._require_approver(user, env)
        return self.store.approve(env_id, user["name"], self.cfg["max_attempts"])

    def reject(self, user, env_id, reason) -> dict:
        env = self._load(user, env_id)
        self._require_approver(user, env)
        return self.store.reject(env_id, user["name"], str(reason or "")[:300])

    @staticmethod
    def _require_approver(user, env):
        if user["role"] not in ("approver", "admin"):
            raise ApiError(403, "approver role required")
        if env["owner"] == user["name"]:
            raise ApiError(403, "you cannot approve your own request")

    def destroy(self, user, env_id) -> dict:
        self._load(user, env_id, write=True)
        return self.store.request_destroy(env_id, user["name"], "requested by " + user["name"], self.cfg["max_attempts"])

    def extend(self, user, env_id, minutes) -> dict:
        env = self._load(user, env_id, write=True)
        tmpl = self.catalog.get(env["template"])
        if tmpl is None:
            raise ApiError(409, "template no longer exists")
        extra, err = policy.evaluate_extension(env, minutes, tmpl, self.cfg, self.store, self.store.clock())
        if err:
            raise ApiError(422, err)
        return self.store.extend(env_id, user["name"], minutes, extra, tmpl.max_extensions)

    def logs_allowed(self, user, env_id):
        return self._load(user, env_id)
