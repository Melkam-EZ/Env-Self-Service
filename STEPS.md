# STEPS: Self-Service Environment Provisioning

## Prerequisites (set these up BEFORE step 1)

| Need | Details |
|---|---|
| EC2 instance | Ubuntu 22.04/24.04 or Amazon Linux 2023, **t3.small (2 vCPU, 2 GB) or bigger**, **10 GB disk**, outbound internet. Inbound: SSH (port 22) from your IP only. The portal is **not** exposed to the internet; you reach it through an SSH tunnel. |
| SSH access | Your key pair `.pem` file: `ssh -i key.pem ubuntu@<public-ip>` (Amazon Linux: `ec2-user@`). |
| Private GitHub repo | Push this code to a **private** repo named `env-self-service`. To clone it on EC2 over HTTPS you need a token: GitHub → Settings → Developer settings → Fine-grained tokens → Generate. Repository access: **only this repo**. Permission: **Contents = Read-only**. Use it as the password when `git clone` asks. |
| AWS permissions (only for steps 8-9, optional) | The portal needs AWS access only for the real AWS templates (`s3-sandbox`, `ec2-dev`, `lambda-api`). Attach an **IAM instance role** to the EC2 instance. In a personal sandbox account use `AdministratorAccess`. For anything shared, allow only: S3, EC2 (+ VPC read), IAM (roles and instance profiles named `envp-*`), Lambda, CloudWatch Logs, SSM (read AMI parameter) and `tag:GetResources`. Steps 1-7 need **no AWS permissions at all**. |
| Your laptop | An SSH client. Port 8080 must be free if you want the browser tunnel. |

Time: about 15 minutes (step 4 waits ~2 minutes). Cost: nothing for steps 1-7; a few cents if you do step 8. Do step 10 when finished.

## Steps (run on the EC2 instance)

```bash
# 1. Get the code (HTTPS URL; username = yours, password = the token)
git clone https://github.com/<you>/env-self-service.git && cd env-self-service

# 2. Unit tests (89 tests, ~10 s; Python standard library only, nothing to install)
python3 -m unittest discover -s tests

# 3. Install git, curl, unzip, Python check, Terraform 1.9.8 (needs sudo)
./scripts/setup.sh

# 4. Prove it works with REAL Terraform and no cloud account (~2 min). Ends with "All 8 checks passed."
./scripts/verify.sh

# 5. Start the portal (first run prints one login token per user, shown once)
./scripts/up.sh
```

**6. Open the portal.** On your **laptop** (new terminal):

```bash
ssh -i key.pem -L 8080:localhost:8080 ubuntu@<public-ip>
# then browse to http://localhost:8080 and paste a token
```

Tokens are also in `data/tokens.txt` on the instance. Users: `alice` and `bob` (developers, different teams), `carol` (approver), `admin`.

**7. Try it (in the browser, then from the CLI):**

1. As `alice`: New environment → template `local-demo`, name `try1`, lifetime 3 minutes. It provisions, shows a countdown, and is destroyed by itself when time runs out.
2. As `alice`: same template, set tier `large`, lifetime 240. The preview says **needs approval** (estimated $16 > $5). Submit.
3. In a second browser window as `carol`: Approvals → Approve. Alice's environment starts.
4. As `bob` (other team): you cannot see or destroy Alice's environment.
5. As `admin`: Admin → Run scan. Expect "No orphaned resources found."

CLI (same API as the portal):

```bash
export ENVP_URL=http://127.0.0.1:8080 ENVP_TOKEN=<alice's token>
python3 -m envp.ctl templates
python3 -m envp.ctl request local-demo try2 --ttl 5 --dry-run     # estimate only
python3 -m envp.ctl request local-demo try2 --ttl 5
python3 -m envp.ctl list
python3 -m envp.ctl extend <env-id> 10
python3 -m envp.ctl destroy <env-id>
```

**8. Optional: real AWS** (role attached, see prerequisites). Costs a few cents:

```bash
aws sts get-caller-identity          # must print your account
./scripts/verify_aws.sh              # creates an S3 bucket via the portal, checks it in AWS, destroys it
```

Or in the portal: template `s3-sandbox` or `lambda-api` (instant) or `ec2-dev` (needs a default VPC in the region; connect with SSM Session Manager). Region defaults to `eu-west-1`; change with `export ENVP_AWS_REGION=us-east-1` before `./scripts/up.sh`.

**9. Optional: no Terraform at all.** `./scripts/down.sh && RUNNER=fake ./scripts/up.sh` runs the whole portal against fake infrastructure.

```bash
# 10. Clean up. Destroys every live environment, then stops the portal. Then terminate the EC2 instance.
./scripts/down.sh --destroy-all
```

Optional browser test (on your laptop or the instance, needs Node 18+): `npm i playwright && npx playwright install chromium`, then `URL=http://127.0.0.1:8080 ALICE=<token> CAROL=<token> node tests/ui_smoke.js`.

## If something goes wrong

| Symptom | Fix |
|---|---|
| `terraform not found` | Run `./scripts/setup.sh`. |
| `setup.sh`: Python 3.9+ needed | Ubuntu 22.04+ and Amazon Linux 2023 are fine. Older images: use a newer AMI. |
| `verify.sh` fails at a step | It prints the last 30 lines of the portal log. Send me that output. |
| Portal page does not load | `./scripts/up.sh` prints the problem; see `data/server.log`. Check the tunnel command is still running on your laptop. |
| "That token was not accepted" | Use a token from `data/tokens.txt`. To reset all tokens: `./scripts/down.sh`, delete `data/users.json` and `data/tokens.txt`, run `./scripts/up.sh`. |
| AWS template stuck in `PROVISION_FAILED` | Open the environment, read the log (usually missing IAM permissions or no default VPC). The portal already destroyed the partial resources. |
| `DESTROY_FAILED` | The reaper retries every 10 minutes. Fix the cause (usually permissions), or run Admin → Run scan to see what is left. |
| Port 8080 in use | `PORT=9000 ./scripts/up.sh` and tunnel that port instead. |
