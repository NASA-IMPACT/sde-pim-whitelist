"""Edge stack: API Gateway REST API + API key/usage plan + CloudFront.

The request front door. A REGIONAL REST API (not edge-optimized — CloudFront sits
in front, so an edge-optimized API would mean double CloudFront) proxies to the
Lambda. Every route requires an API key except ``/health`` (uptime probes) and
the ``/docs`` + ``/openapi.json`` API documentation (public). A
CloudFront distribution fronts the API and forwards ``x-api-key`` + query strings
to the origin; caching is disabled so the usage-plan quota meters every request
(cache hits would otherwise bypass API Gateway).
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

from .naming import REST_API_NAME


class ApiEdgeStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        lambda_function: lambda_.IFunction,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        api = apigw.RestApi(
            self,
            "PimRestApi",
            rest_api_name=REST_API_NAME,
            description="PIMS Whitelist read API",
            endpoint_configuration=apigw.EndpointConfiguration(
                types=[apigw.EndpointType.REGIONAL]
            ),
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                throttling_rate_limit=100,
                throttling_burst_limit=200,
                tracing_enabled=True,
            ),
        )

        integration = apigw.LambdaIntegration(lambda_function)

        # /health — no API key (uptime / deploy-gate probes).
        api.root.add_resource("health").add_method(
            "GET", integration, api_key_required=False
        )

        # Public API documentation — no API key. /docs (Swagger UI) fetches
        # /openapi.json, so both must be reachable without a key.
        api.root.add_resource("docs").add_method(
            "GET", integration, api_key_required=False
        )
        api.root.add_resource("openapi.json").add_method(
            "GET", integration, api_key_required=False
        )

        # Everything else (incl. /fetch_pims_records) requires an API key.
        api.root.add_method("ANY", integration, api_key_required=True)
        api.root.add_resource("{proxy+}").add_method(
            "ANY", integration, api_key_required=True
        )

        api_key = api.add_api_key("PimApiKey", api_key_name="pim-api-default")
        usage_plan = api.add_usage_plan(
            "PimUsagePlan",
            name="pim-usage-plan",
            throttle=apigw.ThrottleSettings(rate_limit=100, burst_limit=200),
            quota=apigw.QuotaSettings(limit=1_000_000, period=apigw.Period.MONTH),
        )
        usage_plan.add_api_key(api_key)
        usage_plan.add_api_stage(stage=api.deployment_stage)

        distribution = cloudfront.Distribution(
            self,
            "PimCdn",
            comment="PIMS Whitelist API",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.RestApiOrigin(api),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                # Disabled so the usage-plan quota counts every request (a cache
                # hit never reaches API Gateway and would not be metered).
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy(
                    self,
                    "ForwardApiKey",
                    origin_request_policy_name="pim-forward-apikey",
                    header_behavior=cloudfront.OriginRequestHeaderBehavior.allow_list(
                        "x-api-key"
                    ),
                    query_string_behavior=cloudfront.OriginRequestQueryStringBehavior.all(),
                    cookie_behavior=cloudfront.OriginRequestCookieBehavior.none(),
                ),
            ),
        )

        CfnOutput(self, "CloudFrontDomain", value=distribution.distribution_domain_name)
        CfnOutput(self, "ApiKeyId", value=api_key.key_id)
        CfnOutput(self, "RestApiUrl", value=api.url)
