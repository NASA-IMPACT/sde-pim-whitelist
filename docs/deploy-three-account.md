# Three-Account Promotion Flow for the PIMS Whitelist API

How to take the current **single-account** CDK deploy and run it as a
**branch-per-environment promotion** across three AWS accounts, mirroring
`sde-elastic-wrapper`'s model. This is the **primary, recommended way to deploy
PIMS**.

The concrete AWS + GitHub facts below are harvested from the sibling repo
**`sde-elastic-wrapper`** (NASA-IMPACT), which already deploys the same style of
Lambda + API Gateway + CloudFront stack into these accounts.

> This document supersedes the single-account assumptions in
> [`deploy.md`](./deploy.md). Where `deploy.md` says "the account," read "the
> account for the environment you're deploying." The CDK steps are otherwise the
> same — you just run them **once per account** with an `env` selector.
>
> **Source of truth for the account facts:**
> `sde-elastic-wrapper/MULTI_ACCOUNT_DEPLOYMENT.md`, `DEPLOYMENT_CHECKLIST.md`,
> `IAM_PERMISSIONS_INVESTIGATION.md`, `bootstrap/*.tfvars`,
> `bootstrap/iam_github.tf`, `.github/workflows/deploy.yml`. That repo uses
> **OpenTofu/Terraform**; PIMS whitelist uses **AWS CDK** — the
> account/region/GitHub facts carry over, the tooling does not.

---

## The accounts

`sde-elastic-wrapper` runs a branch-per-environment model across three accounts,
all in **`us-east-1`**. PIMS reuses the same accounts and branch mapping.

| Env  | AWS Account Name | Account ID     | CLI profile | Git branch | Trust `sub`                                             |
|------|------------------|----------------|-------------|------------|---------------------------------------------------------|
| dev  | UAH AWS          | `998871305517` | `work`      | `dev`      | `repo:NASA-IMPACT/sde-pim-whitelist:ref:refs/heads/dev` |
| test | SMCE Dev         | `119417011911` | `sde-test`  | `test`     | `…:ref:refs/heads/test`                                 |
| prod | SMCE Prod        | `756157247091` | `sde-prod`  | `prod`     | `…:ref:refs/heads/prod`                                 |

- **Region:** `us-east-1` everywhere (matches the `deploy.md` default).
- **Auth:** `sde-test` / `sde-prod` are **AWS SSO** profiles (`aws configure sso`),
  not long-lived IAM keys. dev (`work`) is pre-existing. Verify with
  `aws sts get-caller-identity --profile <profile>` before any CDK step.
- SMCE test SSO role seen in use: `AWSReservedSSO_Project-Admin_4c8b8feda31c727c`
  (admin — satisfies the "admin credentials for one-time bootstrap" prerequisite).

**Key design choice — isolation is by account, not by name.** Every environment
lives in its own AWS account, so the resource names stay identical everywhere
(`pim-api` function, `pim-api` REST API, `pim-api-github-deploy` role,
`pim-api-code-<account>-us-east-1` bucket). No per-env name suffixing — the same
pattern `sde-elastic-wrapper` uses. Nothing in the app stacks changes; only the
**bootstrap trust**, the **CDK env selector**, and the **deploy workflow** do.

> **Branch names:** `dev / test / prod` — `dev` is this repo's existing
> integration branch (the sibling repo calls its equivalent `development`; the
> account IDs and flow are the same). To use different names, it's a pure
> find/replace across the four files below plus `cdk.json`. Note the repo's
> current default branch is still `main` — decide whether to rename `main → dev`
> or keep `main` as the dev branch (then swap `dev`→`main` in the files below and
> in `cdk.json`).

---

## The model

```
feature branch ──PR──►  dev ─────►  test ─────►  prod
                            │                 │            │
                        (merge)           (merge)      (merge)
                            ▼                 ▼            ▼
                     UAH  998871305517   SMCE 119417011911  SMCE 756157247091
                          (dev)              (test)            (prod)
```

---

## What changes (4 files) and what doesn't

| File | Change |
|---|---|
| `infra/cdk.json` | add an `environments` map (env → account + branch) |
| `infra/app.py` | select account/branch by `-c env=<name>`; import-vs-create OIDC |
| `infra/stacks/bootstrap_stack.py` | trust the env's **deploy branch** (not hardcoded `main`); optionally import an existing OIDC provider |
| `.github/workflows/deploy.yml` | trigger on all three branches; pick the GitHub **Environment** by branch |
| `.github/workflows/pr-checks.yml` | run checks for PRs into any long-lived branch |

Unchanged: `naming.py`, `platform_stack.py`, `api_edge_stack.py`,
`scripts/build_lambda_zip.sh`, and all application code.

---

## 1. `infra/cdk.json` — declare the environments

```json
{
  "app": "python app.py",
  "watch": { "include": ["**"], "exclude": ["README.md", "*.pyc", ".venv/**"] },
  "context": {
    "github_owner": "NASA-IMPACT",
    "github_repo": "sde-pim-whitelist",
    "environments": {
      "dev":  { "account": "998871305517", "branch": "dev" },
      "test": { "account": "119417011911", "branch": "test" },
      "prod": { "account": "756157247091", "branch": "prod" }
    },
    "@aws-cdk/core:newStyleStackSynthesis": true,
    "@aws-cdk/aws-lambda:recognizeLayerVersion": true,
    "@aws-cdk/core:checkSecretUsage": true
  }
}
```

Account IDs are not secrets (the sibling repo commits them in its tfvars); keeping
them here pins each `cdk deploy` to a specific account so you can't accidentally
ship dev to the prod account — CDK errors if your ambient credentials don't match.

## 2. `infra/app.py` — full replacement

```python
#!/usr/bin/env python3
"""CDK app entrypoint for the PIMS Whitelist read API (multi-account).

Select the target environment with ``-c env=dev|test|prod`` (default: dev). The
env chosen fixes the AWS account and the git branch whose OIDC token may assume
the deploy role. Resource names are identical in every account — isolation is by
account, so no per-env suffixing.
"""

import aws_cdk as cdk
from stacks.api_edge_stack import ApiEdgeStack
from stacks.bootstrap_stack import BootstrapStack
from stacks.platform_stack import PlatformStack

app = cdk.App()

github_owner = app.node.try_get_context("github_owner") or "NASA-IMPACT"
github_repo = app.node.try_get_context("github_repo") or "sde-pim-whitelist"
region = app.node.try_get_context("region") or "us-east-1"

environments = app.node.try_get_context("environments") or {}
target = app.node.try_get_context("env") or "dev"
if target not in environments:
    raise SystemExit(
        f"unknown env '{target}'; pass -c env=<name> where name in "
        f"{sorted(environments)}"
    )
conf = environments[target]
account = conf["account"]
deploy_branch = conf["branch"]

env = cdk.Environment(account=account, region=region)

# All three target accounts already have a GitHub OIDC provider (created by
# sde-elastic-wrapper). Import it by default; pass -c create_oidc=true only for a
# brand-new account that has none.
create_oidc = str(app.node.try_get_context("create_oidc")).lower() == "true"
oidc_provider_arn = (
    None
    if create_oidc
    else f"arn:aws:iam::{account}:oidc-provider/token.actions.githubusercontent.com"
)

BootstrapStack(
    app,
    "PimApiBootstrapStack",
    env=env,
    github_owner=github_owner,
    github_repo=github_repo,
    deploy_branch=deploy_branch,
    oidc_provider_arn=oidc_provider_arn,
)

platform = PlatformStack(app, "PimApiPlatformStack", env=env)

ApiEdgeStack(app, "PimApiEdgeStack", env=env, lambda_function=platform.function)

app.synth()
```

## 3. `infra/stacks/bootstrap_stack.py` — trust the deploy branch, import OIDC

Change the signature and the two blocks at the top of `__init__`; everything
below the trust policy (the S3/Lambda statements, the output) is unchanged.

```python
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        github_owner: str,
        github_repo: str,
        deploy_branch: str,
        oidc_provider_arn: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Only one OIDC provider per URL is allowed per account. These accounts
        # already have one (sde-elastic-wrapper), so import by default; pass
        # oidc_provider_arn=None (app: -c create_oidc=true) for a fresh account.
        if oidc_provider_arn:
            provider = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
                self, "GitHubOidcProvider", oidc_provider_arn
            )
        else:
            provider = iam.OpenIdConnectProvider(
                self,
                "GitHubOidcProvider",
                url="https://token.actions.githubusercontent.com",
                client_ids=["sts.amazonaws.com"],
            )

        subject = f"repo:{github_owner}/{github_repo}:ref:refs/heads/{deploy_branch}"
```

The rest of the method is exactly as it is today. The audience is
`sts.amazonaws.com`. Confirm the exact provider ARN per account before deploying:

```bash
aws iam list-open-id-connect-providers --profile <profile>
```

The provider already exists at this ARN in each account (created by
`sde-elastic-wrapper/bootstrap/iam_github.tf`):

```
arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com
# e.g. arn:aws:iam::119417011911:oidc-provider/token.actions.githubusercontent.com
```

## 4. `.github/workflows/deploy.yml` — multi-branch + GitHub Environments

Two edits: the `on.push.branches` list, and add an `environment:` to the job so
env-scoped secrets resolve. The steps are otherwise identical (function name
`pim-api` is the same in every account).

```yaml
on:
  push:
    branches: [dev, test, prod]
    paths:
      - "src/**"
      - "whitelist/classified/**"
      - "whitelist/*.txt"
      - "scripts/build_lambda_zip.sh"
      - "pyproject.toml"
      - "uv.lock"
      - ".github/workflows/deploy.yml"

concurrency:
  group: deploy-${{ github.ref_name }}   # was deploy-main — isolate per branch
  cancel-in-progress: false

permissions:
  id-token: write
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    # Resolves to the GitHub Environment whose scoped secrets we use.
    environment: >-
      ${{ github.ref_name == 'prod' && 'prod'
          || github.ref_name == 'test' && 'test'
          || 'dev' }}
    steps:
      # ...unchanged steps... they read secrets.AWS_DEPLOY_ROLE_ARN,
      # secrets.CODE_BUCKET, secrets.CF_DOMAIN — now resolved per Environment.
```

Nothing else in the job body changes: `--function-name pim-api`, the S3 upload,
and the `/health` smoke test all work per-account because the names match.

## 5. `.github/workflows/pr-checks.yml` — gate every long-lived branch

```yaml
on:
  pull_request:
    branches: [dev, test, prod]
```

So a promotion PR (`dev → test`, `test → prod`) runs `pre-commit` +
`pytest` before it can merge, same as feature PRs into `dev`.

---

## One-time setup, per account

Do this **three times** — once per env. Substitute the profile + env name.

```bash
cd infra
source .venv/bin/activate            # cdk.json runs "python app.py"

export AWS_PROFILE=sde-test          # work | sde-test | sde-prod
export CDK_DEFAULT_REGION=us-east-1
aws sts get-caller-identity          # confirm you're in the RIGHT account

# CDKToolkit is per-account and probably absent (sibling repo uses Terraform,
# not CDK, so no account here has ever run `cdk bootstrap` — this is separate
# from the Terraform state backends, which are unrelated; see the appendix):
cdk bootstrap aws://119417011911/us-east-1     # this env's account id

# Deploy the three stacks for THIS env. -c env=<name> picks account + branch.
cdk deploy -c env=test PimApiBootstrapStack    # OIDC imported, role trusts refs/heads/test
cdk deploy -c env=test PimApiPlatformStack     # bucket + placeholder Lambda
cdk deploy -c env=test PimApiEdgeStack         # API GW + CloudFront

# Record outputs → GitHub Environment secrets (next section):
#   PimApiBootstrapStack.DeployRoleArn  -> AWS_DEPLOY_ROLE_ARN
#   PimApiPlatformStack.CodeBucketName  -> CODE_BUCKET   (pim-api-code-<acct>-us-east-1)
#   PimApiEdgeStack.CloudFrontDomain    -> CF_DOMAIN
#   PimApiEdgeStack.ApiKeyId            -> fetch key value later (Part 4 of deploy.md)
```

> Because the OIDC provider is **imported**, `PimApiBootstrapStack` will not try to
> create a second one — this is the fix `deploy.md` Step 4 warns about, applied by
> default for all three accounts. For a genuinely fresh account, add
> `-c create_oidc=true`.

Repeat for `dev` (`AWS_PROFILE=work`, `-c env=dev`, branch `dev`) and
`prod` (`AWS_PROFILE=sde-prod`, `-c env=prod`).

---

## GitHub configuration

### Environments + scoped secrets (recommended over suffixed repo secrets)

**Settings → Environments** → create `dev`, `test`, `prod`. In **each**, add three
**environment secrets** (same names in all three — the Environment scopes them):

| Secret | Value (from that account's CDK outputs) |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | `arn:aws:iam::<acct>:role/pim-api-github-deploy` |
| `CODE_BUCKET` | `pim-api-code-<acct>-us-east-1` |
| `CF_DOMAIN` | that env's `dxxxx.cloudfront.net` |

This beats the sibling repo's `AWS_ROLE_ARN_DEV/TEST/PROD` suffixing because it
also gives you **protection rules** — set **required reviewers** on the `prod`
Environment so a human must approve before the prod deploy job runs, and
optionally restrict the `prod` Environment to the `prod` branch.

Optional repo **variable** `AWS_REGION=us-east-1` (the workflow already defaults
to `us-east-1`).

> The deploy role name in the sibling repo is `github-actions-deployment-role`;
> PIMS names its own `pim-api-github-deploy`, so there's no collision.

### Branch protection

**Settings → Branches** — protect `dev`, `test`, and `prod`:

- Require a PR before merging.
- Require the **`quality`** status check (from `pr-checks.yml`).
- On `prod` (and ideally `test`): require a review. Combined with the `prod`
  Environment's required reviewer, prod gets two gates.

---

## Promotion procedure (day-to-day)

```bash
# 1. Feature work → dev
git checkout dev && git pull
git checkout -b feature/xyz
# ...changes... open PR into dev, merge when quality is green
#   → deploy.yml fires, ships to 998871305517 (dev)

# 2. Promote dev → test
git checkout test && git pull
git merge --ff-only dev        # or open a PR: dev → test
git push                                #   → ships to 119417011911 (test)

# 3. Promote test → prod
git checkout prod && git pull
git merge --ff-only test               # PR recommended; prod Environment gate applies
git push                                #   → ships to 756157247091 (prod, after approval)
```

Because the classified sidecars ship **inside** the zip, a data refresh promotes
through the exact same merges — dev/test/prod converge on identical data at each
promotion, which is what you want.

---

## Verify each environment

Per env (fetch the key once with `aws apigateway get-api-key --api-key <ApiKeyId>
--include-value ... --profile <profile>`):

```bash
CF=<env CloudFront domain>; KEY=<env api key>
curl -fsS "https://$CF/health" | grep -q '"status": "ok"'
curl -s -H "x-api-key: $KEY" "https://$CF/fetch_pims_records?page_size=1" | jq '.total'
```

---

## Gotchas & known failure modes (carried over from `sde-elastic-wrapper`)

1. **OIDC provider already exists in all three accounts** — handled by importing
   (see file change #3). `sde-elastic-wrapper/bootstrap/iam_github.tf` already
   creates it, so `deploy.md` Step 4's *"only one provider per URL per account"*
   warning **will** bite you if you let CDK create a new one. If you ever see
   `EntityAlreadyExists` for the provider, you either forgot that `-c env=` maps
   to an account whose provider must be imported, or you passed
   `-c create_oidc=true` wrongly.

2. **Deploy-role IAM was too thin in the sibling repo.** Terraform/CDK needs
   bucket-attribute reads; the sibling repo's CI failed on `s3:GetBucketPolicy`,
   `s3:GetBucketVersioning`, `s3:GetBucketEncryption`, `s3:GetBucketAcl`,
   `s3:GetBucketLocation`, `s3:GetBucketPublicAccessBlock` until they were added
   (dev only "worked" because of a manually-added `s3:Get*` inline policy that
   masked the gap; they ultimately used `s3:*` + `iam:*` wildcards in test/prod
   to move on). **PIMS's deploy role is deliberately narrow** (`s3:PutObject` on
   `app/*` + `lambda:UpdateFunctionCode` and friends on `pim-api`) — that's
   sufficient for the code-only deploy workflow, since the sibling's
   bucket-attribute failures were a **Terraform** need, not ours. Don't widen it
   unless a real `AccessDenied` on bucket reads actually appears.

3. **Wrong-account footgun.** Always `aws sts get-caller-identity` before `cdk
   deploy`, and rely on the pinned account in `cdk.json` to make CDK refuse a
   mismatched profile.

4. **`workflow_dispatch` defaults to dev.** When triggering a deploy manually you
   must pass the env explicitly, or it deploys to the wrong account.

5. **`main` vs `dev`.** The repo's current default branch is `main`. Decide
   whether to rename `main → dev` or keep `main` as the dev branch (then
   swap `dev`→`main` in the four files above and in `cdk.json`).

6. **State/secret sync churn (only if PIMS adds Secrets Manager later).** In the
   sibling repo, Secrets Manager "scheduled for deletion" and tainted IAM roles
   caused repeated failures; `restore-secret` / untaint fixed them. Not relevant
   today — PIMS reads local JSON sidecars.

7. **OpenSearch Serverless data-access policy (elastic-only, does NOT apply to
   PIMS).** The sibling's Lambda role must be added to the AOSS data access policy
   *after* deploy or health checks 403. PIMS reads local sidecars, so this doesn't
   apply — but if PIMS ever queries the SDE OpenSearch collections, the endpoints
   are in the appendix.

---

## Rollback

Per env, identical to `deploy.md`: every artifact is `app/<git-sha>.zip` in that
account's code bucket.

```bash
AWS_PROFILE=<env profile> aws lambda update-function-code --function-name pim-api \
  --s3-bucket pim-api-code-<acct>-us-east-1 --s3-key app/<previous-sha>.zip --publish
```

Or revert the merge on that env's branch and let the pipeline redeploy.

---

## Appendix: reference facts (sibling repo, for comparison)

### Terraform state backends already present (leave them alone — unrelated to CDK)

These are the sibling repo's OpenTofu state backends. They are **not** the same as
the `CDKToolkit` stack — `cdk bootstrap` creates its own and does not touch these.

| Env  | TF state S3 bucket    | Lock table          |
|------|-----------------------|---------------------|
| dev  | `tf-state-poc-1984`   | `tf-state-lock`     |
| test | `tf-state-sde-test`   | `tf-state-lock-test`|
| prod | `tf-state-sde-prod`   | `tf-state-lock-prod`|

### Working CloudFront domains (sibling app)

Use these only to confirm the account wiring is sane — PIMS gets its own
CloudFront domain from `PimApiEdgeStack`.

| Env  | CloudFront domain                        | Health |
|------|------------------------------------------|--------|
| test | `https://dyejsbdumgpqz.cloudfront.net`   | passing |
| prod | `https://djst9dlcao29r.cloudfront.net`   | passing |

Sibling Lambda code buckets follow `sde-elastic-wrapper-<env>-lambda-code`; CDK
mints its own `pim-api-code-<acct>-us-east-1` for PIMS — don't reuse the elastic one.

### SDE OpenSearch endpoints (only if PIMS ever queries AOSS)

- test: `https://blu7t5amz4xhrhjlwr7f.us-east-1.aoss.amazonaws.com/`
- prod: `https://o2mxw7n9akk8n7o5oiqb.us-east-1.aoss.amazonaws.com/`
