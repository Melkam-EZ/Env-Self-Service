"""Bearer-token auth. Only SHA-256 hashes of tokens are stored."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets

from .service import ROLES

DEFAULT_USERS = [
    ("alice", "payments", "developer"),
    ("bob", "search", "developer"),
    ("carol", "platform", "approver"),
    ("admin", "platform", "admin"),
]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_users_file(path: str) -> dict:
    """Write a new users file and return {name: plaintext token}. Tokens are shown once."""
    tokens, users = {}, []
    for name, team, role in DEFAULT_USERS:
        tok = secrets.token_urlsafe(24)
        tokens[name] = tok
        users.append({"name": name, "team": team, "role": role, "token_sha256": _hash(tok)})
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"users": users}, f, indent=2)
    return tokens


class Auth:
    def __init__(self, users: list):
        for u in users:
            if u.get("role") not in ROLES:
                raise ValueError(f"user {u.get('name')}: role must be one of {ROLES}")
        self.users = users

    @classmethod
    def load(cls, path: str) -> "Auth":
        with open(path) as f:
            return cls(json.load(f)["users"])

    def authenticate(self, header):
        """Authorization header -> user dict or None. Compares every hash in constant time."""
        if not header or not header.startswith("Bearer "):
            return None
        digest = _hash(header[7:].strip())
        found = None
        for u in self.users:
            if hmac.compare_digest(u["token_sha256"], digest):
                found = u
        return found
