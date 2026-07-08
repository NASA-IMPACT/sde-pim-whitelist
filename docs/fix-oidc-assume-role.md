# Fix: deploy job fails with `Not authorized to perform sts:AssumeRoleWithWebIdentity`

The `deploy.yml` job pins `environment: dev`. When a GitHub Actions job references
an Environment, GitHub issues the OIDC token with the **environment-form** `sub`
claim (`repo:NASA-IMPACT/sde-pim-whitelist:environment:dev`) instead of the
branch-ref form (`...:ref:refs/heads/dev`). The original deploy role trusted only
the branch-ref subject, so STS rejected the assume-role.

The fix (`infra/stacks/bootstrap_stack.py`) makes the role trust **both**
subjects. This runbook applies it to the live dev account and re-runs the deploy.

> Both subjects are only ever issued from a deploy on this account's branch, so the
> scoping is unchanged.

> **Second fix bundled in the same redeploy.** `bootstrap_stack.py` also now grants
> the deploy role `s3:GetObject` (not just `s3:PutObject`) on `app/*`.
> `lambda:UpdateFunctionCode` reads the S3 artifact **as the calling principal**, so
> without GetObject the deploy fails with
> `not authorized to perform: s3:GetObject on .../app/<sha>.zip` — *after* OIDC
> already succeeded. Step 1 below applies both fixes at once.

---

## 1. Redeploy the bootstrap stack (updates the role's trust policy)

```bash
cd /Users/bbenson/projects/sde-pim-whitelist/infra
source .venv/bin/activate
export AWS_PROFILE=work
export CDK_DEFAULT_REGION=us-east-1
export CDK_DEFAULT_ACCOUNT=998871305517

# confirm you're on the dev account first
aws sts get-caller-identity        # expect "Account": "998871305517"

# deploy the trust-policy fix
JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 cdk deploy -c env=dev PimApiBootstrapStack
```

When the changeset appears, the IAM diff should show the `GitHubDeployRole` trust
policy gaining the `repo:NASA-IMPACT/sde-pim-whitelist:environment:dev` subject.
Answer `y` to deploy.

## 2. (Optional) Verify the live trust policy took the change

```bash
aws iam get-role --role-name pim-api-github-deploy \
  --query 'Role.AssumeRolePolicyDocument.Statement[0].Condition.StringLike' \
  --profile work
```

You want to see **both** subjects listed:

```
repo:NASA-IMPACT/sde-pim-whitelist:environment:dev
repo:NASA-IMPACT/sde-pim-whitelist:ref:refs/heads/dev
```

And confirm the deploy role can now read the artifact (both actions present):

```bash
aws iam list-role-policies --role-name pim-api-github-deploy --profile work
# then inspect the inline policy and check for s3:PutObject AND s3:GetObject on app/*
```

## 3. Confirm the secret is on the dev Environment

GitHub → **Settings → Environments → dev** → confirm `AWS_DEPLOY_ROLE_ARN`,
`CODE_BUCKET`, and `CF_DOMAIN` are listed there (not just repo-level secrets).

## 4. Re-run the failed deploy job

GitHub → **Actions** tab → open the failed **deploy** run → **Re-run jobs** →
**Re-run failed jobs**. It reuses the same commit; no new push needed.

## 5. Watch it clear the OIDC step

The **Configure AWS credentials (OIDC)** step should now succeed, then
Upload → Update Lambda → Smoke test. Green = dev is live with the real code.

---

## Don't forget: commit the code fix

Step 1 deploys the trust-policy change to AWS directly from your machine, so CI
works immediately. But the edit to `infra/stacks/bootstrap_stack.py` is only in
your working tree — commit and merge it to `dev` so the fix isn't lost on the next
infra redeploy.
