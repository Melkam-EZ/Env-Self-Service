# Architecture

## Components

| Part | File | Job |
|---|---|---|
| Policy | `envp/policy.py`, `envp/catalog.py` | Pure functions. Validate input against the template, estimate cost, decide: refuse, hold for approval, or go. |
| Store | `envp/store.py` | SQLite. Owns the state machine, the job queue and the audit log. Every change is a compare-and-set. |
| Service | `envp/service.py` | Permissions (owner, team, approver, admin) in front of the store. |
| Worker | `envp/worker.py` | Claims a job, holds a lease, heartbeats, runs the runner, records the result. |
| Runner | `envp/runner.py` | `TerraformRunner` (real) and `FakeRunner` (tests, demo). |
| Reaper | `envp/reaper.py` | Enforces every deadline (see below). |
| API | `envp/api.py` | JSON API + static portal, standard library only. |
| Orphans | `envp/orphans.py` | Finds resources nobody owns. |

## State machine

```
PENDING_APPROVAL -> QUEUED | REJECTED | CANCELLED
QUEUED           -> PROVISIONING | CANCELLED
PROVISIONING     -> ACTIVE | PROVISION_FAILED
PROVISION_FAILED -> DESTROYING        (cleanup job queued automatically)
ACTIVE           -> DESTROY_QUEUED    (the user, or the reaper at expiry)
DESTROY_QUEUED   -> DESTROYING -> DESTROYED | DESTROY_FAILED
DESTROY_FAILED   -> DESTROY_QUEUED    (the reaper retries)
```

Each arrow is `UPDATE envs SET status=new WHERE id=? AND status IN (allowed)`. If zero rows change, someone else already moved it and the caller backs off. This is how the API, several workers and the reaper can all act at once without a lock manager.

## A request, step by step

1. **Estimate** (`POST /api/estimate`): the portal calls this on every keystroke. Same code path as create, so the preview cannot disagree with the real decision.
2. **Create** (`POST /api/envs`): policy runs, then the insert re-checks quota and budget inside one transaction. Result: `QUEUED`, or `PENDING_APPROVAL` with the reasons.
3. **Approve**: an approver (not the owner) moves it to `QUEUED`. Unanswered approvals expire after 24 h.
4. **Provision**: a worker claims the job (state `PROVISIONING`), writes `envp.tfvars.json` into `data/work/<env-id>/`, runs `init` then `apply`, reads outputs (sensitive ones are dropped). `ACTIVE`, `expires_at = now + TTL`.
5. **Expire**: the reaper sees `expires_at <= now`, queues a destroy with actor `reaper`. A warning event is emitted 15 minutes earlier.
6. **Destroy**: `terraform destroy`; success only if the state is empty; the working directory is deleted; `DESTROYED`.

## Reliability

- **Retries.** Apply and destroy are idempotent, so they are retried with backoff. After the last apply failure the environment becomes `PROVISION_FAILED` and a destroy job cleans up whatever was partly created.
- **Leases.** A claimed job has `lease_until`; the worker heartbeats while Terraform runs. If the worker dies, the reaper sees the expired lease and requeues the job. A job that keeps killing workers runs out of attempts and fails cleanly.
- **Fencing.** `job_done` and `job_failed` only succeed for the worker that still holds the lease (`WHERE worker=? AND status='running'`). A hung worker that wakes up after takeover is ignored.
- **Failed destroys never end the story.** `DESTROY_FAILED` is retried by the reaper indefinitely; the environment keeps its name and its budget until the infrastructure is really gone.
- **Reaper and worker loops log their own failures** to stderr instead of dying silently.

## Security model

- Authentication: bearer token. `users.json` holds only SHA-256 hashes; comparison is constant time over all users.
- Authorization (in `service.py`): developers see their team's environments and change only their own; approvers decide other people's requests; admins see everything and run orphan scans. Another team's environment looks like "not found".
- Input: schema-validated parameters, unknown keys rejected, names and tags restricted to safe characters. Parameter values go to Terraform in a JSON var-file, so they can never become shell text or flags. The `local-demo` provisioner passes values as environment variables for the same reason.
- Browser: strict CSP (no inline script or style), `X-Frame-Options: DENY`, UI uses `textContent`/DOM APIs only, so stored values cannot execute.
- Cloud: templates never open inbound ports, never use wildcard IAM, encrypt storage, and tag everything (`tests/test_templates.py` enforces these on every template).

## Adding a template

1. Create `templates/<name>/` with Terraform (`*.tf`) that declares the standard variables `env_id`, `env_name`, `team`, `owner`, `expires_at` (and `region` for AWS), and a provider block with the `default_tags` used by the existing templates.
2. Add `template.json`: title, description, `provider`, `params` (typed, with defaults and limits), `ttl`, `cost`, optional `approval_rules`, `max_extensions`.
3. Run the tests. `test_templates.py` checks that every parameter exists as a Terraform variable, every variable has a value or default, tags are present, and the security rules hold. No Python changes needed.

## Where the demo cuts corners (and the production answer)

| Demo | Production |
|---|---|
| SQLite, one process | PostgreSQL, multiple replicas, workers as separate deployments |
| Local Terraform state per environment | S3 backend + DynamoDB lock (already supported by the runner) |
| Static tokens | OIDC SSO, groups to teams/roles |
| Cost = price table | AWS Budgets + Cost Explorer, per-environment tag allocation |
| One AWS account | Account per team via the landing zone; the runner assumes a role per environment |
| Webhook notifications | Slack/Teams/email with owner mapping |
