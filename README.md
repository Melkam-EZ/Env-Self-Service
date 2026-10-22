# Self-Service Environment Provisioning

A portal where developers create their own environments (S3 sandbox, EC2 dev box, Lambda API) without filing a ticket, and where **every environment is temporary, capped in cost, tagged to an owner, and destroyed automatically**. No developer touches Terraform or holds cloud credentials; the platform team controls what can be built by owning the templates and the rules.

```
 developer ──portal / CLI──▶ API ──▶ policy ──▶ needs approval? ──▶ approver
   (token)                    │      quota, budget, TTL, schema          │
                              ▼                                          ▼
                         SQLite state machine + job queue ◀──────────────┘
                              │ lease + heartbeat
                              ▼
                        workers ──▶ terraform apply (one directory + one state per environment)
                              ▲
                  reaper ─────┘  expires TTLs, recovers dead workers, retries failed destroys, warns owners
```

## The lifecycle

`PENDING_APPROVAL` → `QUEUED` → `PROVISIONING` → `ACTIVE` → `DESTROY_QUEUED` → `DESTROYING` → `DESTROYED`

Side exits: `REJECTED`, `CANCELLED`, `PROVISION_FAILED` (cleanup is queued automatically), `DESTROY_FAILED` (retried automatically). The countdown starts when the environment becomes `ACTIVE`, not when it was requested.

## What it guarantees (and the test that proves each)

| Guarantee | How |
|---|---|
| Nothing is created that policy forbids | Input is checked against the template schema; unknown parameters are errors; name, TTL, team quota and monthly budget are enforced inside the database transaction (so two simultaneous requests cannot both squeeze under a limit). |
| Expensive requests need a human | Estimated cost above the threshold, or a template rule (`t3.large`), holds the request. Requesters can never approve their own. Extensions cannot be used to dodge the approval. |
| Environments really disappear | The reaper queues a destroy at expiry. Destroy only counts as done if the state is empty; a failed destroy keeps retrying and never marks the environment gone. |
| A crashed worker loses nothing | Jobs are leased and heartbeated. A dead worker's job goes back to the queue. A "zombie" worker that wakes up late is fenced out and cannot overwrite the new owner's result. |
| Failed provisioning does not leak | Terraform apply is retried with backoff; after the last failure the partial resources are destroyed automatically. |
| Orphans are findable | Every AWS resource is tagged (`managed-by`, `env_id`, `owner`, `team`, `expires_at`). The orphan scan compares tags to the database and also checks local state directories. |
| Isolation | One Terraform working directory and one state per environment. Teams only see and change their own environments. Values reach Terraform through a JSON var-file, never the command line. |
| Safe portal | Tokens stored as SHA-256 hashes, constant-time comparison, strict Content-Security-Policy, UI built without `innerHTML`, 64 KB body limit, audit log of every action. |

## Templates

| Template | Needs AWS | What it builds |
|---|---|---|
| `local-demo` | no | A file on the portal host, via real Terraform. Pretend prices let you try budgets and approvals for free. |
| `s3-sandbox` | yes | Private, encrypted bucket; always deletable (force destroy); objects expire. |
| `lambda-api` | yes | Python Lambda behind an IAM-authenticated function URL, 1-day logs. |
| `ec2-dev` | yes | Amazon Linux 2023 box, no inbound ports, SSM Session Manager access, encrypted disk. `t3.large` needs approval. |

A template is a folder: `template.json` (what the portal shows and enforces) next to plain Terraform. Adding one needs no code change. See `docs/ARCHITECTURE.md`.

## Run it

See **`STEPS.md`** (prerequisites first, then copy-paste commands). Short version on a fresh EC2 instance:

```bash
git clone https://github.com/<you>/env-self-service.git && cd env-self-service
python3 -m unittest discover -s tests && ./scripts/setup.sh && ./scripts/verify.sh && ./scripts/up.sh
```

## Repo layout

```
envp/         the service (standard library only): policy, store, worker, reaper, runner, api, orphans, ctl
portal/       the web UI (plain HTML, CSS, JS)
templates/    one folder per environment type: template.json + Terraform
config/       portal.json: budgets, thresholds, limits
scripts/      setup, up, down, verify (real Terraform, no cloud), verify_aws (optional)
tests/        89 unit/integration tests + ui_smoke.js (optional browser check)
docs/         ARCHITECTURE.md
```

## What was verified, and how

- **89 automated tests** (policy, state machine, worker crash recovery, retries, HTTP API, roles, CLI, XSS payloads, template contracts). Deliberately breaking self-approval, TTL expiry, the budget check and lease fencing each makes tests fail.
- **Real Terraform run**: `scripts/verify.sh` was run end to end against OpenTofu 1.8.5 (same CLI and runner path as Terraform): provision, approval gating, team isolation, destroy, rejection, and automatic expiry by the reaper. It also runs against real Terraform on your EC2 instance (step 4).
- **Portal UI** driven in headless Chromium: sign in, live preview, request, approval by a second user, activation, no console errors.
- **Not run in the build environment**: the three AWS templates were never applied to a real AWS account, and `terraform validate` could not run on them (the provider registry was unreachable). They pass `fmt` and structural/security tests. `scripts/verify_aws.sh` and CI (`terraform validate`) are where that gets proven. Treat the first real AWS run as the real test.

## Key ideas to be able to explain

- **Why a job queue instead of running Terraform inside the request?** Apply takes minutes, can fail halfway and must survive restarts. The request just records intent; workers do the slow part, and the state machine makes every step resumable.
- **Why leases and fencing?** A worker can hang or crash while holding a job. Without a lease nobody could take over; without fencing the old worker could wake up and overwrite the new one's result.
- **Why compare-and-set transitions?** The API, workers and reaper act at the same time. `UPDATE ... WHERE status IN (...)` means only one of them can win a given transition.
- **Why check quota and budget inside the transaction?** Checking first and inserting later lets two concurrent requests both pass. The check and the insert must be one atomic step.
- **Why is the TTL the main cost control?** The most expensive cloud resource is the one somebody forgot. Making expiry the default turns "remember to delete" into "must opt in to keep".
- **Why one state per environment?** A failure or destroy can only ever affect one environment. Shared state means shared blast radius.

## Limitations (deliberate)

- Single portal process with SQLite. Production: PostgreSQL, several portal replicas, remote Terraform state (S3 + DynamoDB lock; the runner already supports `ENVP_STATE_BUCKET`, `ENVP_STATE_LOCK_TABLE`).
- Token login. Production: SSO (OIDC) with groups mapped to teams and roles.
- Costs are estimates from a price table in `template.json`, not the AWS bill. Use AWS Budgets and Cost Explorer for truth.
- Environments run in one AWS account. Production: one account per team or environment, via the landing zone.
- Portal listens on `127.0.0.1`; put it behind HTTPS and your identity provider before exposing it.
