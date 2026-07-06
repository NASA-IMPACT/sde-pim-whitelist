"""Platform stack: S3 code bucket + Lambda + log group.

Owns everything the frequent deploy workflow touches. Keeping the bucket and the
function in one stack avoids a cross-stack export on the bucket ARN. The function
ships with a throwaway inline placeholder; the real code+data zip is delivered by
the deploy workflow via ``aws lambda update-function-code`` (so CDK does not run
on every code change). The handler and environment are set here because
``update-function-code`` only replaces code, not configuration.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from .naming import FUNCTION_NAME, code_bucket_name

_PLACEHOLDER = (
    "def handler(event, context):\n"
    "    # Replaced on first deploy by aws lambda update-function-code.\n"
    "    return {'statusCode': 503, 'body': 'awaiting first deploy'}\n"
)


class PlatformStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.code_bucket = s3.Bucket(
            self,
            "CodeBucket",
            bucket_name=code_bucket_name(),
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-old-artifacts",
                    prefix="app/",
                    noncurrent_version_expiration=Duration.days(30),
                    expiration=Duration.days(90),
                )
            ],
        )

        log_group = logs.LogGroup(
            self,
            "FunctionLogs",
            log_group_name=f"/aws/lambda/{FUNCTION_NAME}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.function = lambda_.Function(
            self,
            "ApiFunction",
            function_name=FUNCTION_NAME,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="pim_whitelist.api.handler.handler",
            code=lambda_.Code.from_inline(_PLACEHOLDER),
            memory_size=1024,
            timeout=Duration.seconds(30),
            environment={
                # Where the bundled sidecars live inside the zip (see build script).
                "PIM_DATA_DIR": "/var/task/whitelist/classified",
                # Restrict to the real UI origin(s) in production.
                "PIM_CORS_ORIGINS": '["*"]',
            },
            log_group=log_group,
            tracing=lambda_.Tracing.ACTIVE,
        )

        CfnOutput(self, "CodeBucketName", value=self.code_bucket.bucket_name)
        CfnOutput(self, "FunctionName", value=self.function.function_name)
