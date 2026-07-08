#!/usr/bin/env python3
"""CDK app entrypoint for the PIMS Whitelist read API (multi-account).

Three deployable stacks:

- ``PimApiBootstrapStack`` — OIDC provider + deploy role (deploy once, admin).
- ``PimApiPlatformStack``  — S3 code bucket + Lambda + logs.
- ``PimApiEdgeStack``      — API Gateway REST + api key/usage plan + CloudFront.

The bootstrap stack is intentionally decoupled (it scopes IAM by resource name,
not by construct reference) so it can be deployed first. The edge stack's only
cross-stack reference is the Lambda function from the platform stack.

Select the target environment with ``-c env=dev|test|prod`` (default: dev). The
env chosen fixes the AWS account and the git branch whose OIDC token may assume
the deploy role. Resource names are identical in every account — isolation is by
account, so no per-env suffixing. Accounts and branches live in cdk.json under
``context.environments``.
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

ApiEdgeStack(
    app,
    "PimApiEdgeStack",
    env=env,
    lambda_function=platform.function,
)

app.synth()
