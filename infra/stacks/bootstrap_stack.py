"""One-time bootstrap: GitHub OIDC provider + scoped deploy role.

Deployed once per account with admin credentials, separately from the app stacks
(you cannot use the deploy role to create the deploy role). The role is what the
`deploy.yml` GitHub Actions workflow assumes via OIDC; its trust is scoped to the
repo's ``main`` ref so only post-merge runs can assume it, and its permissions are
limited to pushing the code artifact and updating the one Lambda.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

from .naming import FUNCTION_NAME, code_bucket_name


class BootstrapStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        github_owner: str,
        github_repo: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # If the account already has a GitHub OIDC provider, import it instead of
        # creating a second one (only one provider per URL is allowed per account):
        #   iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(...)
        provider = iam.OpenIdConnectProvider(
            self,
            "GitHubOidcProvider",
            url="https://token.actions.githubusercontent.com",
            client_ids=["sts.amazonaws.com"],
        )

        subject = f"repo:{github_owner}/{github_repo}:ref:refs/heads/main"
        principal = iam.OpenIdConnectPrincipal(provider).with_conditions(
            {
                "StringEquals": {
                    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                },
                "StringLike": {
                    "token.actions.githubusercontent.com:sub": subject,
                },
            }
        )

        role = iam.Role(
            self,
            "GitHubDeployRole",
            role_name="pim-api-github-deploy",
            assumed_by=principal,
            max_session_duration=Duration.hours(1),
            description="Assumed by GitHub Actions (main branch) to deploy the PIM API",
        )

        bucket_arn = f"arn:aws:s3:::{code_bucket_name()}"
        function_arn = (
            f"arn:aws:lambda:{self.region}:{self.account}:function:{FUNCTION_NAME}"
        )

        # Push the built artifact under app/ in the code bucket.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[f"{bucket_arn}/app/*"],
            )
        )
        # Point the Lambda at the new artifact and publish a version.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "lambda:UpdateFunctionCode",
                    "lambda:GetFunction",
                    # Needed by `aws lambda wait function-updated-v2` (polls
                    # GetFunctionConfiguration) in the deploy workflow.
                    "lambda:GetFunctionConfiguration",
                    "lambda:PublishVersion",
                    "lambda:UpdateFunctionConfiguration",
                ],
                resources=[function_arn],
            )
        )

        CfnOutput(
            self,
            "DeployRoleArn",
            value=role.role_arn,
            description="Set as the AWS_DEPLOY_ROLE_ARN GitHub secret",
        )
