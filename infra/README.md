# PIMS Whitelist API — Infrastructure (AWS CDK, Python)

Provisions the AWS serving stack for the read API. Deploy mechanism:
**CDK provisions static infra once**; GitHub Actions ships code+data on every
merge to `main` via `aws lambda update-function-code` (see
`.github/workflows/deploy.yml`).

## Stacks

| Stack | Owns | Cadence |
|---|---|---|
| `PimApiBootstrapStack` | GitHub OIDC provider + scoped `pim-api-github-deploy` role | once, admin |
| `PimApiPlatformStack` | S3 code bucket, Lambda (`pim-api`), log group | rare (infra changes) |
| `PimApiEdgeStack` | API Gateway REST (regional) + API key/usage plan + CloudFront | rare |

Request path: **CloudFront → API Gateway (API key) → Lambda (Mangum → FastAPI)**.
`/health` is the only key-free route (uptime/deploy probes).

## One-time setup (admin credentials)

```bash
cd infra
python -m venv .venv && .venv/bin/pip install -r requirements.txt
export CDK_DEFAULT_ACCOUNT=<acct> CDK_DEFAULT_REGION=us-east-1

# Set the GitHub org so the deploy role trusts only this repo's main branch:
#   edit cdk.json  ->  context.github_owner = "<your-org>"

npx cdk@2 bootstrap aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION
npx cdk@2 deploy PimApiBootstrapStack     # note the DeployRoleArn output
npx cdk@2 deploy PimApiPlatformStack      # note CodeBucketName output
npx cdk@2 deploy PimApiEdgeStack          # note CloudFrontDomain + ApiKeyId
```

`cdk synth` runs fully offline (no AWS calls) and is the quickest validation:
`.venv/bin/python app.py` writes the templates to `cdk.out/`.

> If the account already has a GitHub OIDC provider, import it in
> `stacks/bootstrap_stack.py` instead of creating a second one (AWS allows one
> provider per URL per account).

## GitHub configuration (for `deploy.yml`)

Repo **secrets**: `AWS_DEPLOY_ROLE_ARN` (bootstrap output), `CODE_BUCKET`
(platform output), `CF_DOMAIN` (edge output). Repo **variable** (optional):
`AWS_REGION`. Make `pr-checks / quality` a required status check on `main`.

The API key value is retrieved from API Gateway (`ApiKeyId` output) and shared
with consumers out-of-band; it is not stored in the repo.

## Deploy flow after setup

`PR → pr-checks (pre-commit + pytest) → merge to main → deploy.yml builds the
Linux-targeted zip → S3 → update-function-code → smoke test /health`.

Infra changes (rare) are applied manually with `npx cdk@2 deploy <stack>` using
admin credentials — CI is not granted infra privileges. Because the deploy
workflow updates the function code out-of-band, re-running `cdk deploy
PimApiPlatformStack` reverts the function to the inline placeholder; re-run the
deploy workflow (or push a trivial change) afterward to restore the live code.
