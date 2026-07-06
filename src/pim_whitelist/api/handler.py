"""AWS Lambda entrypoint.

``handler`` is the function API Gateway invokes; Mangum adapts the ASGI ``app``
to the Lambda proxy event/response shape. The index is built lazily on the first
request (module-scope singleton via ``deps.get_index``) and reused across warm
invocations.
"""

from __future__ import annotations

from mangum import Mangum

from .app import app

handler = Mangum(app)
