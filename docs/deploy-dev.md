# Deploying the PIMS Whitelist API to the AWS **dev** environment

A concrete, copy-paste walkthrough for standing up (and then continuously
deploying) the PIMS Whitelist read API in the **dev** AWS account only.

This is the dev-specific instantiation of the three-account model described in
[`deploy-three-account.md`](./deploy-three-account.md). All the multi-account
code changes it describes (`cdk.json`, `app.py`, `bootstrap_stack.py`, and the
two workflows) are **already applied** in this repo — you do **not** need to edit
any source to deploy to dev. You only run CDK once and set three GitHub secrets.

> Everything below targets exactly one account. Do **not** run these against the
> `test` (`119417011911`) or `prod` (`756157247091`) accounts — see
> `deploy-three-account.md` for those.

---

## The dev target — one table to keep open

| Item | Value |
|---|---|
| AWS account name | **UAH AWS** |
| AWS account ID | **`998871305517`** |
| AWS CLI profile | **`work`** (pre-existing IAM creds, not SSO) |
| Region | **`us-east-1`** |
| Git deploy branch | **`dev`** |
| CDK env selector | **`-c env=dev`** |
| GitHub Environment | **`dev`** |
| Deploy role ARN | `arn:aws:iam::998871305517:role/pim-api-github-deploy` |
| Code bucket | `pim-api-code-998871305517-us-east-1` |
| Lambda / REST API name | `pim-api` (identical in every account — isolation is by account) |

The account ID is pinned in `cdk.json` under `context.environments.dev`, so CDK
will **refuse to deploy** if your ambient credentials point at a different
account. That is your safety net against a wrong-account deploy.

---

## Prerequisites (on the machine doing the one-time setup)

- **AWS CLI v2**, with the **`work`** profile configured and holding
  **admin-level** credentials in account `998871305517` (admin is only needed for
  this one-time bootstrap; CI later uses the tightly-scoped `pim-api-github-deploy`
  role).
- **Node.js 22 or 24 LTS** — the CDK CLI is a Node tool (20 also works but is
  deprecated; newer non-LTS versions like 25 only print an untested-version
  warning — see troubleshooting).
- **Python 3.12**.
- **GitHub access** to `NASA-IMPACT/sde-pim-whitelist` with permission to create
  Environments, add secrets, and set branch protection.

Confirm you are pointed at the dev account **before anything else**:

```bash
aws sts get-caller-identity --profile work
# Expect "Account": "998871305517"
```

If that prints any other account ID, stop and fix your profile.

---

## Part 1 — One-time AWS setup (CDK), dev account

You run this **once**. Day-to-day deploys after this are just merges to `dev`
(Part 3).

### Step 1 — Install the CDK app dependencies

```bash
cd infra
python -m venv .venv
.venv/bin/pip install -r requirements.txt
source .venv/bin/activate          # cdk.json runs "python app.py" — this venv must be active
npm install -g aws-cdk             # or prefix every `cdk` below with: npx cdk@2
```

### Step 2 — Point CDK and the CLI at the dev account

```bash
export AWS_PROFILE=work
export CDK_DEFAULT_REGION=us-east-1
export CDK_DEFAULT_ACCOUNT=998871305517

aws sts get-caller-identity        # RE-CONFIRM: "Account": "998871305517"
```

> **Offline sanity check (no AWS calls):** `cdk synth -c env=dev` (or `python
> app.py`) writes CloudFormation to `cdk.out/`. If this fails, fix it before
> touching AWS.

### Step 3 — Confirm the GitHub OIDC provider already exists

The dev account already has a GitHub OIDC provider (created by the sibling
`sde-elastic-wrapper` repo), and `app.py` **imports it by default**. Verify:

```bash
aws iam list-open-id-connect-providers --profile work
```

You should see:

```
arn:aws:iam::998871305517:oidc-provider/token.actions.githubusercontent.com
```

- **If it's there (expected):** do nothing — the default import path is correct.
- **If it's genuinely absent:** add `-c create_oidc=true` to the
  `PimApiBootstrapStack` deploy in Step 5 so CDK creates one. Do **not** pass this
  flag if the provider already exists, or the deploy fails with
  `EntityAlreadyExists`.

### Step 4 — Bootstrap the dev account for CDK (once per account/region)

The three accounts use Terraform, not CDK, so this account has almost certainly
never run `cdk bootstrap`. This creates the `CDKToolkit` stack (assets bucket +
the roles CDK itself needs). It is unrelated to any Terraform state bucket.

```bash
cdk bootstrap aws://998871305517/us-east-1
```

### Step 5 — Deploy the three stacks to dev

`-c env=dev` fixes the account (`998871305517`) and scopes the deploy role's
trust to `refs/heads/dev`.

```bash
# 1) OIDC import + pim-api-github-deploy role (trusts the dev branch)
cdk deploy -c env=dev PimApiBootstrapStack
#    add -c create_oidc=true ONLY if Step 3 showed no provider

# 2) S3 code bucket + placeholder Lambda + log group
cdk deploy -c env=dev PimApiPlatformStack

# 3) API Gateway REST + API key/usage plan + CloudFront
cdk deploy -c env=dev PimApiEdgeStack
```

The Lambda comes up with a placeholder that returns 503 — expected. The first CI
deploy (Part 3) replaces it with the real code + data. CloudFront takes ~5–15 min
to finish globally on first create.

### Step 6 — Record the four outputs

Copy these from the CDK output (you'll need them in Part 2 and Part 4):

| CDK output | Use it as | Actual dev value (from the initial deploy) |
|---|---|---|
| `PimApiBootstrapStack.DeployRoleArn` | GitHub secret `AWS_DEPLOY_ROLE_ARN` | `arn:aws:iam::998871305517:role/pim-api-github-deploy` |
| `PimApiPlatformStack.CodeBucketName` | GitHub secret `CODE_BUCKET` | `pim-api-code-998871305517-us-east-1` |
| `PimApiEdgeStack.CloudFrontDomain` | GitHub secret `CF_DOMAIN` | `d2vzyrjsfbe2fn.cloudfront.net` |
| `PimApiEdgeStack.ApiKeyId` | fetch key value in Part 4 | `umhd5so9q8` |
| `PimApiEdgeStack.RestApiUrl` | direct origin URL (bypasses CloudFront/API key) | `https://a6sloobw0j.execute-api.us-east-1.amazonaws.com/prod/` |

> These are the concrete outputs recorded from the first `cdk deploy` of the dev
> account. The CloudFront domain and API-key **ID** are stable identifiers (not the
> key **value**, which is fetched out-of-band in Step 9). If you re-create the edge
> stack from scratch they will change — re-read them from the `cdk deploy` output.

---

## Part 2 — Wire up GitHub for the dev environment

### Step 7 — Create the `dev` GitHub Environment + secrets

**Settings → Environments → New environment → `dev`.** Inside the `dev`
Environment, add three **environment secrets** (the workflow's `deploy` job
selects the `dev` Environment automatically for pushes to the `dev` branch):

| Secret | Value (from Step 6) |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | `arn:aws:iam::998871305517:role/pim-api-github-deploy` |
| `CODE_BUCKET` | `pim-api-code-998871305517-us-east-1` |
| `CF_DOMAIN` | `d2vzyrjsfbe2fn.cloudfront.net` |

Optional repo **variable** `AWS_REGION=us-east-1` (the workflow already defaults
to `us-east-1` if unset).

> Using a GitHub **Environment** (not plain repo secrets) matters: it keeps dev's
> values separate from test/prod and lets you add protection rules later without
> reworking anything.

### Step 8 — Protect the `dev` branch

**Settings → Branches → Add rule** for `dev`:

- ✅ Require a pull request before merging.
- ✅ Require status checks to pass → select **`quality`** (the job in
  `pr-checks.yml`, which runs `pre-commit` + `pytest` on PRs into `dev`).

---

## Part 3 — First deploy to dev (and every deploy after)

Deploys to dev are fully automated by `.github/workflows/deploy.yml`, triggered by
a push to the **`dev`** branch that touches `src/**`,
`whitelist/classified/**`, `whitelist/*.txt`, the build script, or deps.

```
open a PR into dev ─► pr-checks (pre-commit + pytest) green ─► merge to dev
                                  │
                                  ▼
        deploy.yml runs, environment = dev:
          1. re-run tests
          2. scripts/build_lambda_zip.sh → app.zip (Linux wheels + code + data)
          3. assume AWS_DEPLOY_ROLE_ARN via OIDC (no stored keys)
          4. aws s3 cp app.zip s3://pim-api-code-998871305517-us-east-1/app/<sha>.zip
          5. aws lambda update-function-code (points pim-api at the new zip)
          6. smoke test https://<CF_DOMAIN>/healthz  → must return "status": "ok"
```

To trigger the very first real deploy:

```bash
git checkout dev
git pull
# merge a feature branch (or a trivial change touching src/**) into dev via PR
git push
```

Watch it under the repo's **Actions** tab. When the `deploy` job is green, dev is
live with the real code + data.

> **Note on the current default branch.** This repo's default branch is still
> `main`; `dev` is the deploy branch for the dev account. Feature PRs should target
> `dev`. (If you later decide `main` *is* dev, you'd swap `dev`→`main` in
> `cdk.json` and the two workflows and re-run Step 5 — not needed for this guide.)

---

## Part 4 — Verify the live dev API

### Step 9 — Fetch the dev API key value

```bash
aws apigateway get-api-key --api-key <ApiKeyId-from-Step-6> \
  --include-value --query value --output text --profile work
```

Share this out-of-band; it is not stored in the repo.

### Step 10 — Call the deployed dev endpoints

```bash
CF=d2vzyrjsfbe2fn.cloudfront.net   # dev CloudFront domain (from Step 6)
KEY=<the dev api key value>

# health probe — no key required
curl -fsS "https://$CF/healthz" | grep -q '"status": "ok"' && echo OK

# a real query — requires the key
curl -s -H "x-api-key: $KEY" \
  "https://$CF/fetch_pims_records?page_size=1" | jq '.total'

# no key -> 403 (auth enforced at API Gateway)
curl -s -o /dev/null -w "%{http_code}\n" "https://$CF/fetch_pims_records"
```

Interactive docs: `https://$CF/docs` (requires the key).

---

## Ongoing dev operations

- **Update data or app code:** merge to `dev`. The classified sidecars ship
  **inside** the zip, so a whitelist/classification refresh redeploys through the
  same pipeline.
- **Change infrastructure (rare):** re-run the relevant `cdk deploy -c env=dev
  <Stack>` with admin creds (`AWS_PROFILE=work`). CI is not granted infra rights.
  > **Drift note:** re-running `cdk deploy -c env=dev PimApiPlatformStack` reverts
  > the Lambda to the inline placeholder. After any platform-stack deploy, re-run
  > the deploy workflow (or push a trivial change to `dev`) to restore live code.
- **Roll back:** every artifact is content-addressed in S3.
  ```bash
  AWS_PROFILE=work aws lambda update-function-code --function-name pim-api \
    --s3-bucket pim-api-code-998871305517-us-east-1 \
    --s3-key app/<previous-git-sha>.zip --publish
  ```

---

## Dev-specific troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `cdk deploy` errors that credentials don't match the account | Your `work` profile isn't in `998871305517`. Re-run `aws sts get-caller-identity --profile work`. The pinned account in `cdk.json` is refusing the mismatch (working as intended). |
| `EntityAlreadyExists` on the OIDC provider | You passed `-c create_oidc=true` but the provider already exists. Drop the flag — the default import path is correct for dev. |
| Bootstrap stack deploy fails on the OIDC provider not found | The provider genuinely doesn't exist. Re-run Step 5's first command **with** `-c create_oidc=true`. |
| Deploy step fails: `Could not assume role with OIDC: Not authorized to perform sts:AssumeRoleWithWebIdentity` | The token's `sub` doesn't match the role's trust policy. Most common cause: the `deploy` job pins `environment: dev`, so GitHub sends the **environment** sub (`repo:NASA-IMPACT/sde-pim-whitelist:environment:dev`), not the branch-ref sub. The bootstrap stack now trusts **both** — if you deployed the role before this fix, re-run `cdk deploy -c env=dev PimApiBootstrapStack` (admin creds) to update the trust policy, then re-run the deploy job. Also verify `AWS_DEPLOY_ROLE_ARN` is set in the **dev Environment** (not repo-level) and the push was on the `dev` branch. |
| `update-function-code` AccessDenied | Re-run `cdk deploy -c env=dev PimApiBootstrapStack` (the deploy role's permissions live there). |
| `/healthz` returns 503 | A sidecar didn't load / the placeholder is still live (no CI deploy yet, or a platform-stack redeploy reverted it). Merge to `dev` to ship real code; check CloudWatch `/aws/lambda/pim-api`. |
| First smoke test fails but function works | CloudFront still propagating (up to ~15 min on first create). Re-run the job. |
| `cdk` prints a big `!!` banner: "not been tested with node vXX" | Benign — you're on a newer Node (e.g. v25) than CDK's tested LTS lines (20/22/24). The deploy still works. Silence it with `export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1`, or install Node 22 LTS to match the Prerequisites. |
| "current credentials could not be used to assume `...cdk-hnb659fds-*-role...`, but are for the right account. Proceeding anyway." | Benign for admin creds — CDK couldn't assume its scoped bootstrap roles so it falls back to your `work` admin identity, which has the rights. Not an error as long as the account ID matches. |
| `SSM parameter /cdk-bootstrap/hnb659fds/version not found. Has the environment been bootstrapped?` | Step 4 (`cdk bootstrap aws://998871305517/us-east-1`) hasn't run yet in this account/region. Run it, then re-run the `cdk deploy`. |

See also `docs/deploy-three-account.md` (full promotion flow) and `docs/deploy.md`
(single-account reference this guide specializes).

---

## Appendix — GitHub CI/CD, step by step

Two workflows drive dev. Neither stores AWS keys; the deploy one authenticates to
AWS with short-lived credentials via **OIDC**.

| Workflow | File | Trigger | AWS access | Purpose |
|---|---|---|---|---|
| **pr-checks** (`quality` job) | `.github/workflows/pr-checks.yml` | PR **into** `dev` (or `test`/`prod`) | none | Gate: `pre-commit` + `pytest` |
| **deploy** (`deploy` job) | `.github/workflows/deploy.yml` | **push** to `dev` (path-filtered) | OIDC → `pim-api-github-deploy` | Ship code + data to the dev Lambda |

### How OIDC auth works (why there are no stored AWS keys)

1. The `deploy` job requests a signed **OIDC token** from GitHub (enabled by
   `permissions: id-token: write` in the workflow).
2. Because the `deploy` job pins `environment: dev`, GitHub issues the token with
   the **environment-form** `sub` claim
   `repo:NASA-IMPACT/sde-pim-whitelist:environment:dev` — **not** the branch-ref
   form `...:ref:refs/heads/dev`. (An `environment:` pin always wins the `sub`
   claim.) The deploy role therefore trusts **both** subjects; both are only ever
   issued from a dev-branch deploy, so the scoping is identical.
3. `aws-actions/configure-aws-credentials@v4` presents it to AWS STS and assumes
   `AWS_DEPLOY_ROLE_ARN`.
4. The `pim-api-github-deploy` role (from `PimApiBootstrapStack`) trusts the dev
   account's OIDC provider **only** for those exact subjects — so only a deploy on
   the `dev` branch/environment of this repo can assume it. A fork, another branch,
   or another repo cannot.
5. STS returns 1-hour credentials scoped to just `s3:PutObject` on `app/*` and a
   few `lambda:*` actions on `pim-api`. Nothing wider.

This is why Part 1 records `DeployRoleArn` and Part 2 sets it as a secret — the
trust (branch → account) is established in AWS, the ARN pointer lives in GitHub.

### The PR gate — `pr-checks.yml`, step by step

Runs on every pull request whose **base** branch is `dev`:

1. `actions/checkout@v4` — check out the PR head.
2. `astral-sh/setup-uv@v5` — install `uv` + Python 3.12.
3. `uv sync --extra api` — install deps (including the API extras).
4. `uv run pre-commit run --all-files` — black / isort / flake8 / pyupgrade.
5. `uv run pytest -q` — the test suite.

Make **`quality`** a required status check on `dev` (Step 8) so a red run blocks
the merge — and therefore blocks the deploy, since deploy only fires *after* merge.

### The deploy — `deploy.yml`, step by step

Runs on **push to `dev`** when the push touches any path filter (`src/**`,
`whitelist/classified/**`, `whitelist/*.txt`, `scripts/build_lambda_zip.sh`,
`pyproject.toml`, `uv.lock`, or the workflow itself). A docs-only merge does
**not** redeploy.

1. The job resolves `environment: dev` (from the `github.ref_name == ...` ternary),
   so `secrets.AWS_DEPLOY_ROLE_ARN`, `secrets.CODE_BUCKET`, and `secrets.CF_DOMAIN`
   come from the **dev** Environment you set up in Step 7.
2. `concurrency: deploy-dev` — serializes dev deploys (`cancel-in-progress: false`),
   so overlapping merges queue instead of racing on the function.
3. `actions/checkout@v4` + `setup-uv@v5`.
4. **Tests** — `uv sync --extra api` then `uv run pytest -q` (re-run on the merged
   result as insurance that what actually ships is green).
5. **Build** — `bash scripts/build_lambda_zip.sh` produces `app.zip` with
   `manylinux2014` wheels + app code + the classified sidecars.
6. **AWS creds** — `configure-aws-credentials@v4` assumes the deploy role via OIDC
   (the flow above), region `us-east-1`.
7. **Upload** — `aws s3 cp app.zip s3://$CODE_BUCKET/app/<GITHUB_SHA>.zip`
   (content-addressed by commit SHA → free rollbacks).
8. **Update** — `aws lambda update-function-code --function-name pim-api
   --s3-bucket $CODE_BUCKET --s3-key app/<sha>.zip --publish`, then
   `aws lambda wait function-updated-v2` to block until the update settles.
9. **Smoke test** — `curl -fsS https://$CF_DOMAIN/healthz` must contain
   `"status": "ok"`, or the job fails red.

### Triggering a deploy manually / re-running

- **Normal:** merge a PR into `dev` (Part 3).
- **Force a redeploy without code changes** (e.g. to overwrite a placeholder after
  a platform-stack redeploy): push an empty-ish change that hits a path filter, or
  from the **Actions** tab open the failed/last **deploy** run and click
  **Re-run jobs** (re-runs on the same commit).
- **Watch it:** repo **Actions** tab → **deploy** workflow → the `dev`-branch run.

### First-run ordering gotcha

The deploy workflow only ships code; it does **not** create infrastructure. So the
very first successful deploy requires Part 1 (CDK: bucket + Lambda + edge) to have
already run, and Part 2 (the three dev secrets) to be set. If the deploy job fails
at the **Configure AWS credentials** step, secrets/role trust are the cause; if it
fails at **Update Lambda code** with `ResourceNotFound`, the platform stack was
never deployed.
