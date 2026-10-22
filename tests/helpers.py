import os
import tempfile
import types
import warnings

from envp.catalog import Catalog
from envp.config import load_config
from envp.reaper import Reaper
from envp.runner import FakeRunner
from envp.service import Service
from envp.store import Store
from envp.worker import LogStore, Worker

warnings.simplefilter("ignore", ResourceWarning)  # in-memory test databases are never closed explicitly

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")

ALICE = {"name": "alice", "team": "payments", "role": "developer"}
DAVE = {"name": "dave", "team": "payments", "role": "developer"}   # alice's teammate
BOB = {"name": "bob", "team": "search", "role": "developer"}
CAROL = {"name": "carol", "team": "platform", "role": "approver"}
ADMIN = {"name": "admin", "team": "platform", "role": "admin"}


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


_KEEP = []  # keep test databases alive until exit so no ResourceWarning noise appears mid-run


def make(cfg=None, runner=None, clock=None):
    clock = clock or Clock()
    c = load_config(overrides=cfg or {})
    store = Store(":memory:", clock=clock)
    catalog = Catalog.load(TEMPLATES)
    runner = runner or FakeRunner()
    logs = LogStore(tempfile.mkdtemp())
    ns = types.SimpleNamespace(
        clock=clock, cfg=c, store=store, catalog=catalog, runner=runner, logs=logs,
        service=Service(store, catalog, c), reaper=Reaper(store, c),
        worker=Worker(store, runner, catalog, c, logs, "w1"),
        worker2=Worker(store, runner, catalog, c, logs, "w2"),
    )
    _KEEP.append(ns)
    return ns


def drain(ctx, limit=50):
    n = 0
    while ctx.worker.run_once() and n < limit:
        n += 1
    return n


def order(ctx, user=ALICE, name="web", template="local-demo", ttl=30, **params):
    return ctx.service.create(user, {"name": name, "template": template, "params": params, "ttl_minutes": ttl})


def active(ctx, user=ALICE, name="web", **kw):
    env = order(ctx, user, name, **kw)
    drain(ctx)
    return ctx.store.get_env(env["id"])
