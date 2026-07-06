"""Read-only HTTP API that serves the classified PIM whitelists.

Additive to the sync CLI: it reads the ``whitelist/classified/*.json`` sidecars
(the :class:`~pim_whitelist.classify.ClassifiedRecord` contract) and serves them
with an optional division filter and pagination. Runs locally under ``uvicorn``
and on AWS Lambda via :data:`pim_whitelist.api.handler.handler` (Mangum).
"""
