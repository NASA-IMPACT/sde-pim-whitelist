#!/usr/bin/env python3
"""CDK app entrypoint for the PIMS Whitelist read API.

Three deployable stacks:

- ``PimApiBootstrapStack`` — OIDC provider + deploy role (deploy once, admin).
- ``PimApiPlatformStack``  — S3 code bucket + Lambda + logs.
- ``PimApiEdgeStack``      — API Gateway REST + api key/usage plan + CloudFront.

The bootstrap stack is intentionally decoupled (it scopes IAM by resource name,
not by construct reference) so it can be deployed first. The edge stack's only
cross-stack reference is the Lambda function from the platform stack.

Set ``github_owner`` (and optionally ``github_repo``) in cdk.json context or via
``-c github_owner=<org>``.
"""

import os

import aws_cdk as cdk
from stacks.api_edge_stack import ApiEdgeStack
from stacks.bootstrap_stack import BootstrapStack
from stacks.platform_stack import PlatformStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

github_owner = app.node.try_get_context("github_owner") or "REPLACE_WITH_GITHUB_ORG"
github_repo = app.node.try_get_context("github_repo") or "sde-pim-whitelist"

BootstrapStack(
    app,
    "PimApiBootstrapStack",
    env=env,
    github_owner=github_owner,
    github_repo=github_repo,
)

platform = PlatformStack(app, "PimApiPlatformStack", env=env)

ApiEdgeStack(
    app,
    "PimApiEdgeStack",
    env=env,
    lambda_function=platform.function,
)

app.synth()
