"""HTTP routes for the PIM read API.

All list endpoints share one code path: pull the (optionally division-filtered)
records for a type — or all types flat — and wrap them in the pagination
envelope. Each response carries an ``ETag`` (data version + query) and
``Cache-Control`` so CloudFront can cache and honor ``If-None-Match`` → 304.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from .deps import get_index, get_settings
from .index import PimIndex
from .models import Page, PageParams, PimType
from .settings import Settings

router = APIRouter(prefix="/fetch_pims_records", tags=["pims"])

IndexDep = Annotated[PimIndex, Depends(get_index)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
ParamsDep = Annotated[PageParams, Query()]


def _serve(
    request: Request,
    response: Response,
    settings: Settings,
    index: PimIndex,
    params: PageParams,
    pim_type: PimType | None,
) -> Page | Response:
    records = index.list(pim_type=pim_type, division=params.division)
    page = Page.build(records, page=params.page, page_size=params.page_size)

    div = params.division.value if params.division else "*"
    typ = pim_type.value if pim_type else "*"
    etag = f'"{index.data_version}-{typ}-{div}-{params.page}-{params.page_size}"'
    cache_control = f"public, max-age={settings.cache_max_age}"

    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": cache_control},
        )
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = cache_control
    return page


@router.get("", response_model=Page, summary="All PIMs (flat, paginated)")
def list_all(
    request: Request,
    response: Response,
    settings: SettingsDep,
    index: IndexDep,
    params: ParamsDep,
) -> Page | Response:
    """All instruments, platforms, and missions as one paginated list.

    Each item carries a ``type`` field. Optional ``division`` filter.
    """
    return _serve(request, response, settings, index, params, None)


@router.get("/instruments", response_model=Page, summary="Instruments")
def list_instruments(
    request: Request,
    response: Response,
    settings: SettingsDep,
    index: IndexDep,
    params: ParamsDep,
) -> Page | Response:
    return _serve(request, response, settings, index, params, PimType.instrument)


@router.get("/platforms", response_model=Page, summary="Platforms")
def list_platforms(
    request: Request,
    response: Response,
    settings: SettingsDep,
    index: IndexDep,
    params: ParamsDep,
) -> Page | Response:
    return _serve(request, response, settings, index, params, PimType.platform)


@router.get("/missions", response_model=Page, summary="Missions")
def list_missions(
    request: Request,
    response: Response,
    settings: SettingsDep,
    index: IndexDep,
    params: ParamsDep,
) -> Page | Response:
    return _serve(request, response, settings, index, params, PimType.mission)
