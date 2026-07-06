"""Shared resource names.

Both the platform stack (which creates the resources) and the bootstrap stack
(which scopes the deploy role's IAM to them) must agree on these names without a
cross-stack dependency, so the bootstrap stack can be deployed first. The code
bucket name embeds the account/region pseudo-parameters so it is globally unique
yet identical across stacks after CloudFormation resolves the tokens.
"""

from __future__ import annotations

from aws_cdk import Aws

FUNCTION_NAME = "pim-api"
REST_API_NAME = "pim-api"


def code_bucket_name() -> str:
    return f"pim-api-code-{Aws.ACCOUNT_ID}-{Aws.REGION}"
