# Deploying the PIMS Whitelist API to AWS

Step-by-step guide to stand up the read API on AWS and wire up GitHub CI/CD.

The model is: **CDK provisions the static infrastructure once** (bucket, Lambda,
API Gateway, CloudFront, IAM), and thereafter **GitHub Actions ships new
code + data on every merge to `main`** via `aws lambda update-function-code`.
You run the CDK steps a handful of times at setup; day-to-day is just merging PRs.

```
CloudFront ──► API Gateway (REST, x-api-key) ──► Lambda (Mangum → FastAPI)
   edge            auth + usage plan               in-memory index over
                                                   whitelist/classified/*.json
```

Three CDK stacks:

| Stack | Creates | When you deploy it |
|---|---|---|
| `PimApiBootstrapStack` | GitHub OIDC provider + `pim-api-github-deploy` IAM role | once (admin) |
| `PimApiPlatformStack` | S3 code bucket, Lambda `pim-api`, log group | once, then only on infra change |
| `PimApiEdgeStack` | API Gateway REST + API key + usage plan + CloudFront | once, then only on infra change |

---

## Prerequisites

Install and configure locally (the machine doing the one-time setup):

- **AWS CLI v2**, configured with **admin credentials** for the target account
  (`aws sts get-caller-identity` should succeed). Admin is needed only for the
  one-time bootstrap; CI later uses a tightly-scoped role.
- **Node.js 20 or 22 LTS** — the CDK CLI is a Node tool. (Newer Node may print an
  "untested" warning.)
- **Python 3.12** and **uv** (`uv --version`).
- **A GitHub repo** for this project with permission to add secrets and branch
  protection.

Pick an AWS **region** (this guide uses `us-east-1`) and note your **account ID**.

---

## Part 1 — One-time AWS setup (CDK)

### Step 1 — Set your GitHub org in the CDK config

Edit `infra/cdk.json` and replace the `github_owner` placeholder with your GitHub
org/user. This scopes the deploy role's trust so **only this repo's `main` branch**
can assume it.

```json
"context": {
  "github_owner": "your-github-org",
  "github_repo": "sde-pim-whitelist"
}
```

### Step 2 — Install the CDK app dependencies

```bash
cd infra
python -m venv .venv
.venv/bin/pip install -r requirements.txt
source .venv/bin/activate          # so `cdk` invokes this venv's python (cdk.json runs "python app.py")
npm install -g aws-cdk             # or prefix every cdk command below with: npx cdk@2
```

Set the target account/region for CDK:

```bash
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-east-1
```

> **Sanity check (offline, no AWS):** `python app.py` writes CloudFormation to
> `cdk.out/` without calling AWS — a quick way to confirm the app synthesizes.

### Step 3 — Bootstrap the account for CDK (once per account/region)

```bash
cdk bootstrap aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION
```

This creates the `CDKToolkit` stack (assets bucket + deploy roles CDK itself needs).

### Step 4 — Deploy the bootstrap stack (OIDC + deploy role)

```bash
cdk deploy PimApiBootstrapStack
```

**Record the output** `PimApiBootstrapStack.DeployRoleArn` — you'll set it as a
GitHub secret in Part 2.

> If the account **already** has a GitHub OIDC provider (only one per URL is
> allowed per account), the deploy fails on the provider. Fix: edit
> `infra/stacks/bootstrap_stack.py` to import the existing provider with
> `iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(...)` instead of
> creating a new one, then redeploy.

### Step 5 — Deploy the platform stack (bucket + Lambda)

```bash
cdk deploy PimApiPlatformStack
```

The Lambda comes up with a throwaway placeholder that returns 503 — that's
expected; the first CI deploy (Part 3) replaces it with the real code+data.

**Record the output** `PimApiPlatformStack.CodeBucketName`.

### Step 6 — Deploy the edge stack (API Gateway + CloudFront)

```bash
cdk deploy PimApiEdgeStack
```

**Record the outputs:**
- `PimApiEdgeStack.CloudFrontDomain` — your public hostname (e.g. `dxxxx.cloudfront.net`).
- `PimApiEdgeStack.ApiKeyId` — the API key's ID (you'll fetch its secret value in Part 4).

CloudFront takes ~5–15 minutes to finish deploying globally the first time.

---

## Part 2 — Configure GitHub CI/CD

### Step 7 — Add repository secrets and variables

In the GitHub repo: **Settings → Secrets and variables → Actions**.

**Secrets** (Repository secrets):

| Secret | Value |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | `PimApiBootstrapStack.DeployRoleArn` from Step 4 |
| `CODE_BUCKET` | `PimApiPlatformStack.CodeBucketName` from Step 5 |
| `CF_DOMAIN` | `PimApiEdgeStack.CloudFrontDomain` from Step 6 |

**Variables** (optional):

| Variable | Value |
|---|---|
| `AWS_REGION` | `us-east-1` (defaults to `us-east-1` if unset) |

### Step 8 — Require the PR checks before merge

**Settings → Branches → Add branch protection rule** for `main`:

- ✅ Require a pull request before merging.
- ✅ Require status checks to pass → select **`quality`** (the job in
  `.github/workflows/pr-checks.yml`).

Now every PR runs `pre-commit` + `pytest` and can't merge until green.

---

## Part 3 — First deployment (and every deployment after)

The deploy is fully automated by `.github/workflows/deploy.yml`. To trigger it:

### Step 9 — Merge to `main`

```
open a PR ─► pr-checks (pre-commit + pytest) passes ─► merge to main
                         │
                         ▼
        deploy.yml runs automatically:
          1. re-run tests
          2. scripts/build_lambda_zip.sh  → app.zip (Linux wheels + code + data)
          3. assume AWS_DEPLOY_ROLE_ARN via OIDC (no stored keys)
          4. aws s3 cp app.zip  s3://$CODE_BUCKET/app/<git-sha>.zip
          5. aws lambda update-function-code  (points pim-api at the new zip)
          6. smoke test  https://$CF_DOMAIN/health
```

Watch it under the repo's **Actions** tab. When the `deploy` job is green, the new
code+data is live. The workflow is path-filtered, so it only fires when
`src/**`, `whitelist/classified/**`, the build script, or deps change — a
docs-only merge won't redeploy.

---

## Part 4 — Verify the live API

### Step 10 — Fetch the API key value

```bash
aws apigateway get-api-key --api-key <ApiKeyId-from-Step-6> \
  --include-value --query value --output text
```

Share this key with consumers out-of-band (it is **not** stored in the repo).

### Step 11 — Call the deployed endpoints

```bash
CF=<your CloudFront domain>
KEY=<the api key value>

# health probe — no key required
curl -s "https://$CF/health"

# a real query — requires the key
curl -s -H "x-api-key: $KEY" \
  "https://$CF/fetch_pims_records/instruments?page=1&page_size=50" | jq '.total'

# division filter + the flat base endpoint
curl -s -H "x-api-key: $KEY" \
  "https://$CF/fetch_pims_records/platforms?division=earth&page_size=100" | jq '.total'
curl -s -H "x-api-key: $KEY" \
  "https://$CF/fetch_pims_records?page_size=100" | jq '.items[0]'

# no key -> 403 (auth enforced at API Gateway)
curl -s -o /dev/null -w "%{http_code}\n" "https://$CF/fetch_pims_records/instruments"
```

Interactive docs: `https://$CF/docs` (requires the key).

---

## Ongoing operations

### Updating the data or app code
Just merge to `main`. The classified sidecars ship **inside** the zip, so a
whitelist/classification refresh (a merged sync PR touching
`whitelist/classified/*.json`) redeploys automatically through the same pipeline.

### Changing infrastructure (rare)
Infra changes are applied manually with admin credentials — CI is **not** granted
infra privileges:

```bash
cd infra && source .venv/bin/activate
cdk diff PimApiEdgeStack        # preview
cdk deploy PimApiEdgeStack
```

> **Drift note:** because CI updates the function code out-of-band, re-running
> `cdk deploy PimApiPlatformStack` reverts the function to the inline placeholder.
> After any platform-stack deploy, re-run the deploy workflow (or push a trivial
> change to `main`) to restore the live code.

### Rolling back
Every artifact is content-addressed in S3 (`app/<git-sha>.zip`). To roll back:

```bash
aws lambda update-function-code --function-name pim-api \
  --s3-bucket <CODE_BUCKET> --s3-key app/<previous-git-sha>.zip --publish
```

Or revert the offending commit on `main` and let the pipeline redeploy.

### Tuning
- **Memory / timeout / architecture:** `infra/stacks/platform_stack.py` (currently
  1024 MB, 30 s, x86_64). If you switch to arm64, also set
  `LAMBDA_ARCH=aarch64` for `scripts/build_lambda_zip.sh`.
- **Throttle / quota:** the usage plan in `infra/stacks/api_edge_stack.py`.
- **CORS:** `PIM_CORS_ORIGINS` env var on the Lambda (set in `platform_stack.py`).
  Restrict it to the real UI origin(s) in production instead of `*`.
- **Cache TTL:** defaults to 300 s; override by adding `PIM_CACHE_MAX_AGE` to the
  Lambda's `environment` in `platform_stack.py`.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `/health` returns 503 | A sidecar didn't load. Check the zip bundled `whitelist/classified/*.json` at the root; check CloudWatch logs `/aws/lambda/pim-api`. |
| Cold-start `invalid ELF header` / `_pydantic_core` import error | The zip was built with non-Linux wheels. The build script pins `manylinux2014`; ensure `LAMBDA_ARCH` matches the CDK function architecture. |
| Deploy workflow fails assuming the role | `AWS_DEPLOY_ROLE_ARN` wrong, or the OIDC trust `sub` doesn't match — it only trusts `refs/heads/main` of the org/repo in `cdk.json`. |
| `update-function-code` AccessDenied | Redeploy `PimApiBootstrapStack` (the deploy role's permissions live there). |
| 403 on every request | Missing/invalid `x-api-key`, or CloudFront isn't forwarding it — the edge stack's origin-request policy forwards `x-api-key`. |
| First deploy smoke test fails but function works | CloudFront still propagating (up to ~15 min on first create). Re-run the job. |

See also `docs/read-api-architecture.md` (design rationale) and `infra/README.md`
(condensed infra reference).
