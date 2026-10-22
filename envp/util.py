from __future__ import annotations

import datetime as dt
import secrets


class ApiError(Exception):
    """An error that maps to an HTTP status code."""

    def __init__(self, status: int, message: str, details=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


def iso(ts):
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def month_start(ts: float) -> float:
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return dt.datetime(d.year, d.month, 1, tzinfo=dt.timezone.utc).timestamp()


def new_env_id() -> str:
    return "env-" + secrets.token_hex(4)
