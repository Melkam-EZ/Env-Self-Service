import threading
import time
import unittest

from envp.runner import FakeRunner
from envp.store import Store
from envp.util import ApiError

from helpers import ADMIN, ALICE, BOB, CAROL, DAVE, Clock, active, drain, make, order


def events(ctx, env_id):
    return [e["action"] for e in ctx.store.events_for(env_id)]


class HappyPath(unittest.TestCase):
    def test_request_to_destroyed(self):
        ctx = make()
        env = order(ctx, ttl=30)
        self.assertEqual(env["status"], "QUEUED")
        self.assertEqual(ctx.store.job_for(env["id"])["kind"], "provision")
        drain(ctx)
        env = ctx.store.get_env(env["id"])
        self.assertEqual(env["status"], "ACTIVE")
        self.assertEqual(env["expires_at"], ctx.clock() + 30 * 60)  # TTL starts when it is READY
        self.assertIn(env["id"], ctx.runner.live)
        self.assertIn("url", env["outputs"])

        ctx.clock.advance(29 * 60)
        self.assertEqual(ctx.reaper.run_once()["expired"], [])
        ctx.clock.advance(61)
        self.assertEqual(ctx.reaper.run_once()["expired"], [env["id"]])
        self.assertEqual(ctx.store.get_env(env["id"])["status"], "DESTROY_QUEUED")
        drain(ctx)
        env = ctx.store.get_env(env["id"])
        self.assertEqual(env["status"], "DESTROYED")
        self.assertEqual(ctx.runner.live, {})
        self.assertEqual(env["destroyed_at"], ctx.clock())
        why = [e for e in ctx.store.events_for(env["id"]) if e["action"] == "destroy_requested"][0]
        self.assertEqual((why["actor"], "expired" in why["detail"]), ("reaper", True))
        self.assertEqual(events(ctx, env["id"])[0], "requested")
        self.assertIn("ready", events(ctx, env["id"]))

    def test_ttl_clock_starts_at_ready_not_at_request(self):
        ctx = make()
        env = order(ctx, ttl=30)
        ctx.clock.advance(600)  # waited 10 minutes in the queue
        drain(ctx)
        self.assertEqual(ctx.store.get_env(env["id"])["expires_at"], ctx.clock() + 30 * 60)

    def test_manual_destroy_charges_actual_usage(self):
        ctx = make()
        env = active(ctx, ttl=120, tier="small")  # $0.50/h pretend, committed $1.00
        self.assertAlmostEqual(env["cost_usd"], 1.0)
        ctx.clock.advance(30 * 60)
        ctx.service.destroy(ALICE, env["id"])
        drain(ctx)
        self.assertAlmostEqual(ctx.store.get_env(env["id"])["cost_usd"], 0.25)

    def test_expiry_warning_sent_once(self):
        ctx = make()
        env = active(ctx, ttl=60)
        ctx.clock.advance(44 * 60)
        self.assertEqual(ctx.reaper.run_once()["warned"], [])
        ctx.clock.advance(2 * 60)  # 14 minutes left, window is 15
        self.assertEqual(ctx.reaper.run_once()["warned"], [env["id"]])
        self.assertEqual(ctx.reaper.run_once()["warned"], [])

    def test_listeners_see_the_important_events(self):
        ctx = make()
        seen = []
        ctx.store.listeners.append(lambda action, env: seen.append((action, env["status"])))
        env = order(ctx, tier="large", ttl=120)
        ctx.service.approve(CAROL, env["id"])
        drain(ctx)
        ctx.clock.advance(120 * 60 - 600)
        ctx.reaper.run_once()
        ctx.clock.advance(700)
        ctx.reaper.run_once()
        drain(ctx)
        actions = [a for a, _ in seen]
        for want in ("approval_requested", "approved", "ready", "expiry_warning", "destroyed"):
            self.assertIn(want, actions)

    def test_a_broken_listener_cannot_break_state_changes(self):
        ctx = make()
        ctx.store.listeners.append(lambda a, e: 1 / 0)
        env = active(ctx)
        self.assertEqual(env["status"], "ACTIVE")


class Approvals(unittest.TestCase):
    def test_expensive_request_waits_for_a_human(self):
        ctx = make()
        env = order(ctx, tier="large", ttl=120)  # $8 > $5 threshold
        self.assertEqual(env["status"], "PENDING_APPROVAL")
        self.assertIsNone(ctx.store.job_for(env["id"]))
        self.assertFalse(ctx.worker.run_once())
        self.assertEqual(ctx.runner.calls, [])

    def test_only_approvers_and_never_your_own(self):
        ctx = make()
        env = order(ctx, tier="large", ttl=120)
        for who in (ALICE, DAVE):
            with self.assertRaises(ApiError) as cm:
                ctx.service.approve(who, env["id"])
            self.assertEqual(cm.exception.status, 403)
        mine = order(ctx, CAROL, "carols", tier="large", ttl=120)
        with self.assertRaises(ApiError) as cm:
            ctx.service.approve(CAROL, mine["id"])
        self.assertIn("own request", cm.exception.message)
        with self.assertRaises(ApiError) as cm:
            ctx.service.approve(ADMIN, order(ctx, ADMIN, "adm", tier="large", ttl=120)["id"])
        self.assertEqual(cm.exception.status, 403)

    def test_approve_then_provision(self):
        ctx = make()
        env = order(ctx, tier="large", ttl=120)
        env = ctx.service.approve(CAROL, env["id"])
        self.assertEqual((env["status"], env["approved_by"]), ("QUEUED", "carol"))
        drain(ctx)
        self.assertEqual(ctx.store.get_env(env["id"])["status"], "ACTIVE")

    def test_cannot_approve_twice_or_after_rejection(self):
        ctx = make()
        env = order(ctx, tier="large", ttl=120)
        ctx.service.reject(CAROL, env["id"], "too big")
        with self.assertRaises(ApiError) as cm:
            ctx.service.approve(CAROL, env["id"])
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(ctx.store.get_env(env["id"])["reason"], "too big")
        self.assertEqual(ctx.runner.calls, [])

    def test_unanswered_request_times_out(self):
        ctx = make()
        env = order(ctx, tier="large", ttl=120)
        ctx.clock.advance(23 * 3600)
        self.assertEqual(ctx.reaper.run_once()["approvals_timed_out"], [])
        ctx.clock.advance(2 * 3600)
        self.assertEqual(ctx.reaper.run_once()["approvals_timed_out"], [env["id"]])
        self.assertEqual(ctx.store.get_env(env["id"])["status"], "REJECTED")

    def test_cancel_before_provisioning_never_touches_the_cloud(self):
        ctx = make()
        env = order(ctx)
        ctx.service.destroy(ALICE, env["id"])
        self.assertEqual(ctx.store.get_env(env["id"])["status"], "CANCELLED")
        self.assertFalse(ctx.worker.run_once())
        self.assertEqual(ctx.runner.calls, [])
        self.assertEqual(ctx.store.job_for(env["id"])["status"], "cancelled")

    def test_cancelled_requests_do_not_count_against_the_budget(self):
        ctx = make({"team_monthly_budget_usd": {"default": 1.0}})
        env = order(ctx, ttl=120)  # $1.00, the whole budget
        ctx.service.destroy(ALICE, env["id"])
        order(ctx, name="again", ttl=120)


class Failures(unittest.TestCase):
    def test_transient_failure_is_retried_with_backoff(self):
        ctx = make(runner=FakeRunner(fail={("provision", "*"): 2}))
        env = order(ctx)
        self.assertTrue(ctx.worker.run_once())
        self.assertEqual(ctx.store.get_env(env["id"])["status"], "PROVISIONING")
        self.assertFalse(ctx.worker.run_once())          # backoff: not due yet
        ctx.clock.advance(5)
        self.assertTrue(ctx.worker.run_once())            # second failure
        self.assertFalse(ctx.worker.run_once())
        ctx.clock.advance(5)
        self.assertFalse(ctx.worker.run_once())           # backoff doubled to 10s
        ctx.clock.advance(5)
        self.assertTrue(ctx.worker.run_once())            # third attempt succeeds
        env = ctx.store.get_env(env["id"])
        self.assertEqual(env["status"], "ACTIVE")
        self.assertEqual(events(ctx, env["id"]).count("provision_retry"), 2)
        self.assertEqual(ctx.store.job_for(env["id"])["attempts"], 3)

    def test_permanent_failure_cleans_up_after_itself(self):
        ctx = make(runner=FakeRunner(fail={("provision", "*"): 99}))
        env = order(ctx)
        for _ in range(3):
            ctx.worker.run_once()
            ctx.clock.advance(60)
        env = ctx.store.get_env(env["id"])
        self.assertEqual(env["status"], "PROVISION_FAILED")
        self.assertIn("provision failed", env["reason"])
        self.assertEqual(ctx.store.job_for(env["id"])["kind"], "destroy")  # cleanup queued automatically
        drain(ctx)
        env = ctx.store.get_env(env["id"])
        self.assertEqual(env["status"], "DESTROYED")
        self.assertEqual(env["cost_usd"], 0.0)
        self.assertEqual(ctx.runner.live, {})
        self.assertIn("provision_failed", events(ctx, env["id"]))

    def test_failed_destroy_is_retried_forever_by_the_reaper(self):
        ctx = make(runner=FakeRunner(fail={("destroy", "*"): 4}))
        env = active(ctx)
        ctx.service.destroy(ALICE, env["id"])
        for _ in range(3):
            ctx.worker.run_once()
            ctx.clock.advance(60)
        self.assertEqual(ctx.store.get_env(env["id"])["status"], "DESTROY_FAILED")
        self.assertIn(env["id"], ctx.runner.live)  # still exists: someone must care
        ctx.clock.advance(5 * 60)
        self.assertEqual(ctx.reaper.run_once()["destroy_retried"], [])      # too soon
        ctx.clock.advance(6 * 60)
        self.assertEqual(ctx.reaper.run_once()["destroy_retried"], [env["id"]])
        ctx.worker.run_once()                                               # 4th failure overall
        ctx.clock.advance(60)
        ctx.reaper.run_once()
        ctx.clock.advance(11 * 60)
        ctx.reaper.run_once()
        drain(ctx)
        self.assertEqual(ctx.store.get_env(env["id"])["status"], "DESTROYED")
        self.assertEqual(ctx.runner.live, {})

    def test_destroy_while_provisioning_is_refused(self):
        ctx = make(runner=FakeRunner(fail={("provision", "*"): 1}))
        env = order(ctx)
        ctx.worker.run_once()  # fails once, env stays PROVISIONING
        with self.assertRaises(ApiError) as cm:
            ctx.service.destroy(ALICE, env["id"])
        self.assertEqual(cm.exception.status, 409)

    def test_removed_template_fails_provision_cleanly(self):
        ctx = make()
        env = order(ctx)
        del ctx.catalog.templates["local-demo"]
        for _ in range(3):
            ctx.worker.run_once()
            ctx.clock.advance(60)
        self.assertEqual(ctx.store.get_env(env["id"])["status"], "PROVISION_FAILED")


class CrashRecovery(unittest.TestCase):
    def test_dead_worker_job_is_taken_over(self):
        ctx = make()
        env = order(ctx)
        job = ctx.store.claim_job("dead-worker", 60)  # claims, then "crashes"
        self.assertEqual(ctx.store.get_env(env["id"])["status"], "PROVISIONING")
        self.assertFalse(ctx.worker.run_once())       # lease still valid: nobody else may take it
        ctx.clock.advance(61)
        self.assertEqual(ctx.reaper.run_once()["recovered_jobs"], 1)
        self.assertTrue(ctx.worker.run_once())
        self.assertEqual(ctx.store.get_env(env["id"])["status"], "ACTIVE")
        self.assertEqual(ctx.store.job_for(env["id"])["attempts"], 2)

    def test_zombie_worker_cannot_overwrite_the_new_owner(self):
        ctx = make()
        env = order(ctx)
        zombie = ctx.store.claim_job("zombie", 60)
        ctx.clock.advance(61)
        ctx.reaper.run_once()
        ctx.worker.run_once()                                       # a healthy worker finishes it
        expires = ctx.store.get_env(env["id"])["expires_at"]
        ctx.clock.advance(100)
        self.assertFalse(ctx.store.job_done(zombie["id"], "zombie", {"url": "stale"}))   # fenced out
        self.assertIsNone(ctx.store.job_failed(zombie["id"], "zombie", "boom", 5))
        after = ctx.store.get_env(env["id"])
        self.assertEqual((after["status"], after["expires_at"]), ("ACTIVE", expires))
        self.assertNotEqual(after["outputs"].get("url"), "stale")

    def test_zombie_cannot_finish_a_job_the_new_owner_is_still_running(self):
        """Only the worker identity can stop the zombie here: the job is 'running', just not for it."""
        ctx = make()
        env = order(ctx)
        zombie = ctx.store.claim_job("zombie", 60)
        ctx.clock.advance(61)
        ctx.reaper.run_once()
        owner = ctx.store.claim_job("w2", 60)                       # new owner is mid-run, has not finished
        self.assertEqual(owner["id"], zombie["id"])
        self.assertFalse(ctx.store.job_done(zombie["id"], "zombie", {"url": "stale"}))
        self.assertIsNone(ctx.store.job_failed(zombie["id"], "zombie", "boom", 5))
        self.assertEqual(ctx.store.get_env(env["id"])["status"], "PROVISIONING")   # untouched by the zombie
        self.assertTrue(ctx.store.job_done(owner["id"], "w2", {"url": "real"}))    # the real owner still can
        self.assertEqual(ctx.store.get_env(env["id"])["outputs"]["url"], "real")

    def test_a_job_that_keeps_killing_workers_gives_up(self):
        ctx = make()
        env = order(ctx)
        for _ in range(3):
            ctx.store.claim_job("doomed", 60)
            ctx.clock.advance(61)
            ctx.reaper.run_once()
        self.assertEqual(ctx.store.get_env(env["id"])["status"], "PROVISION_FAILED")
        self.assertEqual(ctx.store.job_for(env["id"])["kind"], "destroy")

    def test_heartbeat_keeps_a_slow_job_alive(self):
        ctx = make({"lease_seconds": 1}, runner=FakeRunner(delay=2.5), clock=time.time)
        env = order(ctx)
        t = threading.Thread(target=ctx.worker.run_once)
        t.start()
        recovered = 0
        for _ in range(4):
            time.sleep(0.6)
            recovered += ctx.store.requeue_stale(5)
        t.join()
        self.assertEqual(recovered, 0)
        self.assertEqual(ctx.store.get_env(env["id"])["status"], "ACTIVE")
        self.assertEqual(ctx.runner.calls.count(("provision", env["id"])), 1)


class Concurrency(unittest.TestCase):
    def test_each_job_runs_exactly_once_with_many_workers(self):
        runner = FakeRunner(delay=0.01)
        ctx = make({"max_active_per_team": 50, "team_monthly_budget_usd": {"default": 999}}, runner=runner, clock=time.time)
        ids = [order(ctx, name=f"e{i}", ttl=30)["id"] for i in range(25)]
        stop = threading.Event()
        from envp.worker import Worker
        workers = [Worker(ctx.store, runner, ctx.catalog, ctx.cfg, ctx.logs, f"w{i}") for i in range(6)]
        threads = [threading.Thread(target=w.run_forever, args=(stop, 0.01)) for w in workers]
        for t in threads:
            t.start()
        deadline = time.time() + 20
        while time.time() < deadline and any(ctx.store.get_env(i)["status"] != "ACTIVE" for i in ids):
            time.sleep(0.05)
        stop.set()
        for t in threads:
            t.join(5)
        self.assertTrue(all(ctx.store.get_env(i)["status"] == "ACTIVE" for i in ids))
        for i in ids:
            self.assertEqual(runner.calls.count(("provision", i)), 1)

    def test_only_one_open_job_per_environment(self):
        ctx = make()
        env = active(ctx)
        ctx.service.destroy(ALICE, env["id"])
        ctx.service.destroy(ALICE, env["id"])   # idempotent: no second job
        n = ctx.store.db.execute("SELECT COUNT(*) FROM jobs WHERE env_id=? AND kind='destroy'", (env["id"],)).fetchone()[0]
        self.assertEqual(n, 1)


class Extensions(unittest.TestCase):
    def test_extend_moves_deadline_and_cost(self):
        ctx = make()
        env = active(ctx, ttl=30)
        env2 = ctx.service.extend(ALICE, env["id"], 30)
        self.assertEqual(env2["expires_at"], env["expires_at"] + 1800)
        self.assertEqual(env2["extensions"], 1)
        self.assertAlmostEqual(env2["cost_usd"], env["cost_usd"] + 0.25)

    def test_extension_limits(self):
        ctx = make()
        env = active(ctx, ttl=30)
        ctx.service.extend(ALICE, env["id"], 10)
        ctx.service.extend(ALICE, env["id"], 10)
        with self.assertRaises(ApiError) as cm:
            ctx.service.extend(ALICE, env["id"], 10)   # local-demo allows 2
        self.assertIn("limit", cm.exception.message)
        ctx2 = make()
        env = active(ctx2, ttl=200)
        with self.assertRaises(ApiError) as cm:
            ctx2.service.extend(ALICE, env["id"], 60)  # 260 > template max 240
        self.assertIn("lifetime", cm.exception.message)
        for bad in (0, -5, "10", True, 1.5, None):
            with self.assertRaises(ApiError):
                ctx2.service.extend(ALICE, env["id"], bad)

    def test_extending_cannot_dodge_approval(self):
        ctx = make({"approval_cost_threshold_usd": 0.5})
        env = active(ctx, ttl=30)                         # $0.25: no approval
        with self.assertRaises(ApiError) as cm:
            ctx.service.extend(ALICE, env["id"], 60)      # would total $0.75
        self.assertIn("approval threshold", cm.exception.message)

    def test_only_active_environments_extend(self):
        ctx = make()
        env = order(ctx)
        with self.assertRaises(ApiError):
            ctx.service.extend(ALICE, env["id"], 10)


class Quotas(unittest.TestCase):
    def test_team_limit_and_name_uniqueness(self):
        ctx = make({"max_active_per_team": 2})
        order(ctx, name="one")
        with self.assertRaises(ApiError) as cm:
            order(ctx, DAVE, "one")                       # same team, same name
        self.assertIn("one", str(cm.exception.details))
        order(ctx, DAVE, "two")
        with self.assertRaises(ApiError) as cm:
            order(ctx, name="three")
        self.assertIn("quota", cm.exception.details)
        order(ctx, BOB, "one")                            # another team may reuse the name

    def test_name_reusable_after_destroy(self):
        ctx = make()
        env = active(ctx, name="reuse")
        ctx.service.destroy(ALICE, env["id"])
        drain(ctx)
        order(ctx, name="reuse")

    def test_budget_is_a_hard_stop_and_refunds_unused_time(self):
        ctx = make({"team_monthly_budget_usd": {"default": 1.0}})
        first = active(ctx, name="aaa", ttl=120)            # $1.00 committed = the whole budget
        with self.assertRaises(ApiError) as cm:
            order(ctx, name="bbb", ttl=60)
        self.assertIn("budget", cm.exception.details)
        ctx.service.destroy(ALICE, first["id"])           # destroyed straight away: ~$0 actually used
        drain(ctx)
        order(ctx, name="bbb", ttl=60)

    def test_budget_is_per_team_and_per_month(self):
        ctx = make({"team_monthly_budget_usd": {"default": 1.0}})
        active(ctx, name="aaa", ttl=120)
        order(ctx, BOB, "bbb", ttl=120)                     # other team has its own budget
        ctx.clock.advance(40 * 86400)                     # next month: fresh budget
        order(ctx, name="ccc", ttl=120)

    def test_limits_are_rechecked_inside_the_transaction(self):
        ctx = make()
        kw = dict(name="x", team="payments", owner="alice", template="local-demo", params={}, ttl_minutes=30,
                  hourly=0.5, cost=0.25, reasons=[], max_attempts=3, month_start=0)
        with self.assertRaises(ApiError) as cm:
            ctx.store.create_env(max_open=0, budget_limit=100, **kw)       # a racing request that passed policy
        self.assertEqual(cm.exception.status, 409)
        with self.assertRaises(ApiError):
            ctx.store.create_env(max_open=9, budget_limit=0.1, **kw)
        self.assertEqual(ctx.store.list_envs(), [])


class Permissions(unittest.TestCase):
    def test_visibility_and_ownership(self):
        ctx = make()
        env = active(ctx)
        with self.assertRaises(ApiError) as cm:
            ctx.service.get(BOB, env["id"])
        self.assertEqual(cm.exception.status, 404)         # other team: it does not exist for you
        self.assertEqual(ctx.service.get(DAVE, env["id"])["id"], env["id"])   # teammate may look
        with self.assertRaises(ApiError) as cm:
            ctx.service.destroy(DAVE, env["id"])           # ...but not delete
        self.assertEqual(cm.exception.status, 403)
        with self.assertRaises(ApiError):
            ctx.service.extend(DAVE, env["id"], 10)
        self.assertEqual(ctx.service.get(CAROL, env["id"])["id"], env["id"])  # approvers see everything
        ctx.service.destroy(ADMIN, env["id"])              # admins may delete anyone's
        self.assertEqual(ctx.store.get_env(env["id"])["status"], "DESTROY_QUEUED")

    def test_lists_are_scoped(self):
        ctx = make()
        a = order(ctx, name="aaa")
        b = order(ctx, BOB, "bbb")
        self.assertEqual([e["id"] for e in ctx.service.list(ALICE)], [a["id"]])
        self.assertEqual([e["id"] for e in ctx.service.list(BOB)], [b["id"]])
        self.assertEqual(len(ctx.service.list(CAROL)), 2)
        self.assertEqual([e["id"] for e in ctx.service.list(CAROL, mine=True)], [])


if __name__ == "__main__":
    unittest.main()
