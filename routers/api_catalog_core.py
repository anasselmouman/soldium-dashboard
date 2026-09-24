# -*- coding: utf-8 -*-
"""HTTP API for Soldium Catalog core (Phase 2 + Phase 3 execution sources)."""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from catalog_core.db import catalog_transaction
from catalog_core.errors import (
    CatalogApplyError,
    CatalogConflictError,
    CatalogError,
    CatalogNotFoundError,
    CatalogPublishError,
    CatalogValidationError,
)
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.service import CatalogCoreService

router = APIRouter(prefix="/api/soldium-catalog", tags=["soldium-catalog"])


def _http_error(exc: CatalogError) -> HTTPException:
    if isinstance(exc, CatalogNotFoundError):
        return HTTPException(status_code=404, detail=exc.message)
    if isinstance(exc, (CatalogApplyError, CatalogPublishError)):
        return HTTPException(
            status_code=400,
            detail={
                "message": exc.message,
                "code": exc.code,
                "details": exc.details,
            },
        )
    if isinstance(exc, CatalogValidationError):
        return HTTPException(status_code=400, detail=exc.message)
    if isinstance(exc, CatalogConflictError):
        return HTTPException(status_code=409, detail=exc.message)
    return HTTPException(status_code=400, detail=exc.message)


class CreateNodeBody(BaseModel):
    name_ar: str = Field(..., min_length=1, max_length=500)
    note_ar: str = ""
    parent_entry_id: str | None = None


class UpdateNodeBody(BaseModel):
    name_ar: str | None = Field(default=None, min_length=1, max_length=500)
    note_ar: str | None = None


class MoveBody(BaseModel):
    parent_entry_id: str | None = None
    before_entry_id: str | None = None
    after_entry_id: str | None = None


class CreateServiceBody(BaseModel):
    name_ar: str = Field(..., min_length=1, max_length=500)
    note_ar: str = ""
    status: Literal["draft", "active", "archived"] = "draft"
    service_type: str | None = None
    ordering_mode: str | None = None
    min_quantity: int | None = None
    max_quantity: int | None = None
    fulfillment_mode: str | None = None
    target_platform_key: str | None = None
    target_section_key: str | None = None
    target_subsection_key: str | None = None
    target_link_prompt_key: str | None = None
    target_link_type: str | None = None
    parent_entry_id: str | None = None


class UpdateServiceBody(BaseModel):
    name_ar: str | None = Field(default=None, min_length=1, max_length=500)
    note_ar: str | None = None
    status: Literal["draft", "active", "archived"] | None = None
    service_type: str | None = None
    ordering_mode: str | None = None
    min_quantity: int | None = None
    max_quantity: int | None = None
    fulfillment_mode: str | None = None
    target_platform_key: str | None = None
    target_section_key: str | None = None
    target_subsection_key: str | None = None
    target_link_prompt_key: str | None = None
    target_link_type: str | None = None


class ReorderBody(BaseModel):
    position: Literal["up", "down", "before", "after"]
    relative_entry_id: str | None = None


class RestoreBody(BaseModel):
    status: Literal["draft", "active"] = "draft"


class ChangeSourceBody(BaseModel):
    provider_slug: str = Field(..., min_length=1, max_length=200)
    provider_account_key: str = Field(..., min_length=1, max_length=200)
    external_service_id: str = Field(..., min_length=1, max_length=500)


class ChangePriceBody(BaseModel):
    amount_dh: str = Field(..., min_length=1, max_length=40)
    pricing_mode: str = Field(..., min_length=1, max_length=40)
    currency: str | None = "MAD"


@router.get("/tree")
async def get_tree(include_archived: bool = False):
    try:
        with catalog_transaction() as conn:
            tree = CatalogCoreService(conn).get_tree(include_archived=include_archived)
        return {"ok": True, "tree": tree}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.get("/entries")
async def list_entries(parent_entry_id: str | None = None, include_archived: bool = False):
    try:
        with catalog_transaction() as conn:
            items = CatalogCoreService(conn).list_children(
                parent_entry_id, include_archived=include_archived
            )
        return {"ok": True, "entries": items}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.post("/nodes")
async def create_node(body: CreateNodeBody):
    try:
        with catalog_transaction() as conn:
            node = CatalogCoreService(conn).create_node(
                name_ar=body.name_ar,
                note_ar=body.note_ar,
                parent_entry_id=body.parent_entry_id,
            )
        return {"ok": True, "node": node.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.patch("/nodes/{node_id}")
async def update_node(node_id: str, body: UpdateNodeBody):
    try:
        with catalog_transaction() as conn:
            node = CatalogCoreService(conn).rename_node(
                node_id,
                name_ar=body.name_ar,
                note_ar=body.note_ar,
            )
        return {"ok": True, "node": node.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.post("/nodes/{node_id}/move")
async def move_node(node_id: str, body: MoveBody):
    try:
        with catalog_transaction() as conn:
            node = CatalogCoreService(conn).move_node(
                node_id,
                new_parent_entry_id=body.parent_entry_id,
                before_entry_id=body.before_entry_id,
                after_entry_id=body.after_entry_id,
            )
        return {"ok": True, "node": node.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.post("/nodes/{node_id}/archive")
async def archive_node(node_id: str):
    try:
        with catalog_transaction() as conn:
            node = CatalogCoreService(conn).archive_node(node_id)
        return {"ok": True, "node": node.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.get("/commercial-options")
async def commercial_options():
    return {"ok": True, **CatalogCoreService.commercial_options()}


@router.get("/services")
async def list_services(
    status: str | None = None,
    search: str | None = None,
    source: str | None = Query(
        default=None,
        description="none | assigned | active",
    ),
    service_type: str | None = None,
    ordering_mode: str | None = None,
    price: str | None = Query(
        default=None,
        description="none | assigned | active",
    ),
    pricing_mode: str | None = None,
    readiness: str | None = Query(
        default=None,
        description="ready | needs_review",
    ),
    under_entry_id: str | None = Query(
        default=None,
        description="Catalog Structure node entry id — services under this node (descendants)",
    ),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
):
    offset = (page - 1) * limit
    try:
        with catalog_transaction() as conn:
            items, total = CatalogCoreService(conn).list_services(
                status=status,
                search=search,
                source=source,
                service_type=service_type,
                ordering_mode=ordering_mode,
                price=price,
                pricing_mode=pricing_mode,
                readiness=readiness,
                under_entry_id=under_entry_id,
                limit=limit,
                offset=offset,
            )
        pages = max(1, (total + limit - 1) // limit)
        return {
            "ok": True,
            "services": [s.to_dict() for s in items],
            "total_items": total,
            "current_page": page,
            "total_pages": pages,
        }
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.get("/services/{service_id}")
async def get_service(service_id: str):
    try:
        with catalog_transaction() as conn:
            svc = CatalogCoreService(conn).get_service(service_id)
        return {"ok": True, "service": svc.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.post("/services")
async def create_service(body: CreateServiceBody):
    try:
        with catalog_transaction() as conn:
            svc = CatalogCoreService(conn).create_service(
                name_ar=body.name_ar,
                note_ar=body.note_ar,
                status=body.status,
                service_type=body.service_type,
                ordering_mode=body.ordering_mode,
                min_quantity=body.min_quantity,
                max_quantity=body.max_quantity,
                fulfillment_mode=body.fulfillment_mode,
                target_platform_key=body.target_platform_key,
                target_section_key=body.target_section_key,
                target_subsection_key=body.target_subsection_key,
                target_link_prompt_key=body.target_link_prompt_key,
                target_link_type=body.target_link_type,
                parent_entry_id=body.parent_entry_id,
            )
        return {"ok": True, "service": svc.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.patch("/services/{service_id}")
async def update_service(service_id: str, body: UpdateServiceBody):
    data = body.model_dump(exclude_unset=True)
    try:
        with catalog_transaction() as conn:
            svc = CatalogCoreService(conn).update_service(service_id, **data)
        return {"ok": True, "service": svc.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.post("/services/{service_id}/move")
async def move_service(service_id: str, body: MoveBody):
    try:
        with catalog_transaction() as conn:
            svc = CatalogCoreService(conn).move_service(
                service_id,
                new_parent_entry_id=body.parent_entry_id,
                before_entry_id=body.before_entry_id,
                after_entry_id=body.after_entry_id,
            )
        return {"ok": True, "service": svc.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.post("/services/{service_id}/archive")
async def archive_service(service_id: str):
    try:
        with catalog_transaction() as conn:
            svc = CatalogCoreService(conn).archive_service(service_id)
        return {"ok": True, "service": svc.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.get("/services/{service_id}/delete-preview")
async def delete_service_preview(service_id: str):
    try:
        with catalog_transaction() as conn:
            impact = CatalogCoreService(conn).preview_service_delete(service_id)
        return {"ok": True, "preview": impact}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.post("/services/{service_id}/delete")
async def delete_service(service_id: str):
    """Admin delete — archive-based soft delete (never physical row removal)."""
    try:
        with catalog_transaction() as conn:
            result = CatalogCoreService(conn).delete_service(service_id)
        return {"ok": True, **result.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.post("/services/{service_id}/restore")
async def restore_service(service_id: str, body: RestoreBody | None = None):
    status = body.status if body else "draft"
    try:
        with catalog_transaction() as conn:
            svc = CatalogCoreService(conn).restore_service(service_id, status=status)
        return {"ok": True, "service": svc.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.get("/services/{service_id}/execution-source")
async def get_execution_source(service_id: str):
    try:
        with catalog_transaction() as conn:
            svc = CatalogCoreService(conn)
            current = svc.get_execution_source(service_id)
            history = svc.list_execution_source_history(service_id)
            events = svc.list_execution_source_events(service_id)
        return {
            "ok": True,
            "current": current.to_dict() if current else None,
            "history": [h.to_dict() for h in history],
            "events": events,
        }
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.get("/services/{service_id}/execution-source/history")
async def get_execution_source_history(service_id: str):
    try:
        with catalog_transaction() as conn:
            history = CatalogCoreService(conn).list_execution_source_history(service_id)
        return {"ok": True, "history": [h.to_dict() for h in history]}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.post("/services/{service_id}/execution-source")
async def change_execution_source(request: Request, service_id: str, body: ChangeSourceBody):
    actor = getattr(request.state, "admin_username", None)
    # #region agent log
    import json as _json
    import time as _time
    import traceback as _tb
    from pathlib import Path as _Path

    def _dbg(hypothesis_id: str, message: str, data: dict) -> None:
        try:
            payload = {
                "sessionId": "3df71b",
                "runId": "pre-fix",
                "hypothesisId": hypothesis_id,
                "location": "api_catalog_core.py:change_execution_source",
                "message": message,
                "data": data,
                "timestamp": int(_time.time() * 1000),
            }
            line = _json.dumps(payload, ensure_ascii=False, default=str)
            for p in (
                _Path("/tmp/debug-3df71b.log"),
                _Path("/opt/soldium/debug-3df71b.log"),
            ):
                try:
                    with p.open("a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                except Exception:
                    pass
            import logging as _logging

            _logging.getLogger("soldium.debug").warning("DBG %s %s %s", hypothesis_id, message, data)
        except Exception:
            pass

    _dbg(
        "D",
        "endpoint enter",
        {
            "service_id": service_id,
            "provider_slug": body.provider_slug,
            "provider_account_key": body.provider_account_key,
            "external_service_id": body.external_service_id,
            "actor": str(actor) if actor else None,
        },
    )
    # #endregion
    try:
        with catalog_transaction() as conn:
            svc = CatalogCoreService(conn)
            result = svc.change_execution_source(
                service_id,
                provider_slug=body.provider_slug,
                provider_account_key=body.provider_account_key,
                external_service_id=body.external_service_id,
                changed_by=str(actor) if actor else None,
            )
            # #region agent log
            _dbg(
                "A",
                "change_execution_source ok",
                {
                    "service_id": service_id,
                    "unchanged": result.unchanged,
                    "external": getattr(result.current, "external_service_id", None),
                    "wt": (result.legacy_write_through or {}).get("outcome")
                    if isinstance(result.legacy_write_through, dict)
                    else None,
                },
            )
            # #endregion
            readiness = svc.get_service_readiness(service_id).to_dict()
            from catalog_core.publication import CatalogPublicationService

            publication = CatalogPublicationService(conn).get_publication_status(
                service_id
            )
            # #region agent log
            _dbg(
                "A",
                "readiness_publication ok",
                {
                    "service_id": service_id,
                    "ready_state": readiness.get("state"),
                    "pub": publication.get("publication_status")
                    if isinstance(publication, dict)
                    else None,
                },
            )
            # #endregion
        return {
            "ok": True,
            **result.to_dict(),
            "readiness": readiness,
            "publication": publication,
        }
    except CatalogError as exc:
        # #region agent log
        _dbg(
            "D",
            "CatalogError",
            {
                "service_id": service_id,
                "exc_type": type(exc).__name__,
                "message": getattr(exc, "message", str(exc)),
            },
        )
        # #endregion
        raise _http_error(exc) from exc
    except Exception as exc:
        # #region agent log
        _dbg(
            "A",
            "unhandled exception",
            {
                "service_id": service_id,
                "exc_type": type(exc).__name__,
                "exc_msg": str(exc),
                "traceback": _tb.format_exc(),
            },
        )
        # #endregion
        raise


@router.get("/services/{service_id}/price")
async def get_price(service_id: str):
    try:
        with catalog_transaction() as conn:
            svc = CatalogCoreService(conn)
            current = svc.get_price(service_id)
            history = svc.list_price_history(service_id)
        return {
            "ok": True,
            "current": current.to_dict() if current else None,
            "history": [h.to_dict() for h in history],
        }
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.get("/services/{service_id}/readiness")
async def get_service_readiness(service_id: str):
    try:
        with catalog_transaction() as conn:
            result = CatalogCoreService(conn).get_service_readiness(service_id)
        return {"ok": True, "readiness": result.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


class PublishBody(BaseModel):
    expected_content_fingerprint: str | None = None


@router.get("/services/{service_id}/publication")
async def get_service_publication_status(service_id: str):
    from catalog_core.publication import CatalogPublicationService

    try:
        with catalog_transaction() as conn:
            status = CatalogPublicationService(conn).get_publication_status(service_id)
        return {"ok": True, **status}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.get("/services/{service_id}/publication-preview")
async def publication_preview(service_id: str):
    from catalog_core.publication import CatalogPublicationService

    try:
        with catalog_transaction() as conn:
            preview = CatalogPublicationService(conn).preview(service_id)
        return {"ok": True, "preview": preview.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.post("/services/{service_id}/publish")
async def publish_service(
    service_id: str,
    request: Request,
    body: PublishBody | None = None,
):
    from catalog_core.publication import CatalogPublicationService

    payload = body or PublishBody()
    actor = getattr(request.state, "admin_username", None)
    try:
        with catalog_transaction() as conn:
            result = CatalogPublicationService(conn).publish(
                service_id,
                published_by=str(actor) if actor else None,
                expected_content_fingerprint=payload.expected_content_fingerprint,
            )
        return {"ok": True, **result.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.post("/services/{service_id}/unpublish")
async def unpublish_service(service_id: str, request: Request):
    from catalog_core.publication import CatalogPublicationService

    actor = getattr(request.state, "admin_username", None)
    try:
        with catalog_transaction() as conn:
            result = CatalogPublicationService(conn).unpublish(
                service_id,
                published_by=str(actor) if actor else None,
            )
        return {"ok": True, **result.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.get("/services/{service_id}/publications")
async def list_service_publications(
    service_id: str,
    limit: int = Query(default=50, ge=1, le=200),
):
    from catalog_core.publication import CatalogPublicationService

    try:
        with catalog_transaction() as conn:
            items = CatalogPublicationService(conn).list_publications(
                service_id, limit=limit
            )
        return {"ok": True, "publications": [p.to_dict() for p in items]}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.get("/review/summary")
async def review_summary():
    try:
        with catalog_transaction() as conn:
            summary = CatalogCoreService(conn).review_summary()
        return {"ok": True, "summary": summary}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.get("/review")
async def review_list(
    status: str | None = None,
    search: str | None = None,
    service_type: str | None = None,
    ordering_mode: str | None = None,
    readiness: str | None = Query(default="needs_review"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
):
    offset = (page - 1) * limit
    if readiness == "":
        readiness = None
    try:
        with catalog_transaction() as conn:
            svc = CatalogCoreService(conn)
            items, total = svc.list_services(
                status=status,
                search=search,
                service_type=service_type,
                ordering_mode=ordering_mode,
                readiness=readiness,
                include_readiness=False,
                limit=limit,
                offset=offset,
            )
            detailed = []
            for item in items:
                result = evaluate_service_readiness(
                    svc.repo,
                    item,
                    source=item.current_source,
                    price=item.current_price,
                )
                row = item.to_dict()
                row["readiness"] = result.to_dict()
                detailed.append(row)
        pages = max(1, (total + limit - 1) // limit)
        return {
            "ok": True,
            "services": detailed,
            "total_items": total,
            "current_page": page,
            "total_pages": pages,
        }
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.get("/services/{service_id}/price/history")
async def get_price_history(service_id: str):
    try:
        with catalog_transaction() as conn:
            history = CatalogCoreService(conn).list_price_history(service_id)
        return {"ok": True, "history": [h.to_dict() for h in history]}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.post("/services/{service_id}/price")
async def change_price(service_id: str, body: ChangePriceBody):
    try:
        with catalog_transaction() as conn:
            result = CatalogCoreService(conn).change_price(
                service_id,
                amount_dh=body.amount_dh,
                pricing_mode=body.pricing_mode,
                currency=body.currency,
            )
        return {"ok": True, **result.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.post("/entries/{entry_id}/reorder")
async def reorder_entry(entry_id: str, body: ReorderBody):
    try:
        with catalog_transaction() as conn:
            entry = CatalogCoreService(conn).reorder_entry(
                entry_id,
                position=body.position,
                relative_entry_id=body.relative_entry_id,
            )
        return {"ok": True, "entry": entry.to_dict(include_children=False)}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.get("/provider-discovery")
async def provider_catalog_discovery(
    provider_slug: str = Query(..., min_length=1),
    account_key: str = Query(..., min_length=1),
):
    """Read-only Provider Catalog discovery (Phase 6A). Does not mutate the database."""
    from catalog_core.provider_discovery import discover_provider_catalog

    result = await discover_provider_catalog(
        provider_slug=provider_slug,
        account_key=account_key,
    )
    status = 200 if result.ok else 502
    if result.error and result.error.code in {"not_found"}:
        status = 404
    elif result.error and result.error.code in {"auth_config", "unsupported"}:
        status = 400
    return JSONResponse(
        status_code=status,
        content={"ok": result.ok, "discovery": result.to_dict()},
    )


@router.post("/provider-discovery/snapshot")
async def provider_catalog_discover_snapshot(
    provider_slug: str = Query(..., min_length=1),
    account_key: str = Query(..., min_length=1),
):
    """Discover + persist snapshot + diff (Phase 6B). Never mutates SOLDIUM Catalog."""
    from catalog_core.provider_snapshot import discover_snapshot_and_diff

    try:
        with catalog_transaction() as conn:
            result = await discover_snapshot_and_diff(
                conn,
                provider_slug=provider_slug,
                account_key=account_key,
            )
    except CatalogError as exc:
        raise _http_error(exc) from exc

    status = 200 if result.ok else 502
    if not result.ok and result.discovery.error:
        code = result.discovery.error.code
        if code == "not_found":
            status = 404
        elif code in {"auth_config", "unsupported"}:
            status = 400
        elif result.snapshot is None and "مكررة" in (result.message_ar or ""):
            status = 400
    return JSONResponse(
        status_code=status,
        content=result.to_dict(),
    )


@router.get("/provider-snapshots/latest")
async def provider_catalog_latest_snapshot(
    provider_slug: str = Query(..., min_length=1),
    account_key: str = Query(..., min_length=1),
):
    """Latest successful Provider Catalog snapshot for an account (Phase 6B)."""
    from catalog_core.provider_snapshot import ProviderSnapshotRepository

    with catalog_transaction() as conn:
        repo = ProviderSnapshotRepository(conn)
        snap = repo.get_latest_successful_snapshot(provider_slug, account_key)
        items = []
        if snap is not None:
            items = [
                i.to_dict()
                for i in repo.load_snapshot_items(
                    snap.id,
                    provider_slug=snap.provider_slug,
                    provider_account_key=snap.provider_account_key,
                )
            ]
    if snap is None:
        return {"ok": True, "snapshot": None, "items": []}
    return {"ok": True, "snapshot": snap.to_dict(), "items": items}


@router.get("/provider-review/summary")
async def provider_review_summary(
    provider_slug: str | None = None,
    account_key: str | None = None,
):
    """Provider Catalog Review Inbox summary (Phase 6C) — persisted snapshots only."""
    from catalog_core.provider_review import build_provider_review_summary

    with catalog_transaction() as conn:
        summary = build_provider_review_summary(
            conn,
            provider_slug=provider_slug,
            provider_account_key=account_key,
        )
    return {"ok": True, "summary": summary.to_dict()}


@router.get("/provider-review")
async def provider_review_list(
    provider_slug: str | None = None,
    account_key: str | None = None,
    change_type: str | None = Query(
        default=None, description="new | changed | missing"
    ),
    search: str | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Provider Catalog Review Inbox list (Phase 6C). No Provider API calls."""
    from catalog_core.provider_review import CHANGE_TYPE_LABELS_AR, list_provider_review_items

    if change_type is not None and change_type not in CHANGE_TYPE_LABELS_AR:
        raise HTTPException(status_code=400, detail="نوع التغيير غير صالح")

    offset = (page - 1) * limit
    with catalog_transaction() as conn:
        items, total, contexts = list_provider_review_items(
            conn,
            provider_slug=provider_slug,
            provider_account_key=account_key,
            change_type=change_type,  # type: ignore[arg-type]
            search=search,
            limit=limit,
            offset=offset,
        )
    pages = max(1, (total + limit - 1) // limit)
    return {
        "ok": True,
        "items": [i.to_dict() for i in items],
        "total_items": total,
        "current_page": page,
        "total_pages": pages,
        "accounts": [c.to_dict() for c in contexts],
        "note_ar": (
            "هذه تغييرات اكتشفها المزود، وليست تغييرات تم تطبيقها على كتالوج Soldium."
        ),
    }


class ProviderMappingBody(BaseModel):
    provider_slug: str = Field(..., min_length=1)
    provider_account_key: str = Field(..., min_length=1)
    external_service_id: str = Field(..., min_length=1)
    soldium_service_id: str = Field(..., min_length=1)


class ProviderUnmapBody(BaseModel):
    provider_slug: str = Field(..., min_length=1)
    provider_account_key: str = Field(..., min_length=1)
    external_service_id: str = Field(..., min_length=1)


@router.get("/provider-mappings")
async def list_provider_mappings(
    provider_slug: str | None = None,
    account_key: str | None = None,
    soldium_service_id: str | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
):
    from catalog_core.provider_mapping import ProviderMappingService

    offset = (page - 1) * limit
    try:
        with catalog_transaction() as conn:
            items, total = ProviderMappingService(conn).list_active_mappings(
                provider_slug=provider_slug,
                provider_account_key=account_key,
                soldium_service_id=soldium_service_id,
                limit=limit,
                offset=offset,
            )
        pages = max(1, (total + limit - 1) // limit)
        return {
            "ok": True,
            "mappings": [m.to_dict() for m in items],
            "total_items": total,
            "current_page": page,
            "total_pages": pages,
        }
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.get("/provider-mappings/lookup")
async def lookup_provider_mapping(
    provider_slug: str = Query(..., min_length=1),
    account_key: str = Query(..., min_length=1),
    external_service_id: str = Query(..., min_length=1),
):
    from catalog_core.provider_mapping import ProviderMappingService

    with catalog_transaction() as conn:
        status = ProviderMappingService(conn).mapping_status_payload(
            provider_slug=provider_slug,
            provider_account_key=account_key,
            external_service_id=external_service_id,
        )
    return {"ok": True, **status}


@router.get("/provider-mappings/history")
async def provider_mapping_history(
    provider_slug: str | None = None,
    account_key: str | None = None,
    external_service_id: str | None = None,
    soldium_service_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
):
    from catalog_core.provider_mapping import ProviderMappingService

    try:
        with catalog_transaction() as conn:
            history = ProviderMappingService(conn).list_mapping_history(
                provider_slug=provider_slug,
                provider_account_key=account_key,
                external_service_id=external_service_id,
                soldium_service_id=soldium_service_id,
                limit=limit,
            )
        return {"ok": True, "history": [h.to_dict() for h in history]}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.post("/provider-mappings")
async def create_or_change_provider_mapping(body: ProviderMappingBody):
    """Create/change mapping only — never modifies execution sources."""
    from catalog_core.provider_mapping import ProviderMappingService

    try:
        with catalog_transaction() as conn:
            result = ProviderMappingService(conn).create_or_change_mapping(
                provider_slug=body.provider_slug,
                provider_account_key=body.provider_account_key,
                external_service_id=body.external_service_id,
                soldium_service_id=body.soldium_service_id,
            )
        return {"ok": True, **result.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.post("/provider-mappings/unmap")
async def unmap_provider_mapping(body: ProviderUnmapBody):
    from catalog_core.provider_mapping import ProviderMappingService

    try:
        with catalog_transaction() as conn:
            result = ProviderMappingService(conn).end_mapping(
                provider_slug=body.provider_slug,
                provider_account_key=body.provider_account_key,
                external_service_id=body.external_service_id,
            )
        return {"ok": True, **result.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


class ProviderApplyBody(BaseModel):
    """Optional concurrency fingerprint from preview (current source identity)."""

    expect_no_current_source: bool = False
    expected_current_provider_slug: str | None = None
    expected_current_provider_account_key: str | None = None
    expected_current_external_service_id: str | None = None


@router.get("/provider-mappings/{mapping_id}/apply-preview")
async def provider_mapping_apply_preview(mapping_id: str):
    """Read-only Apply preview — no mutations, no Provider API."""
    from catalog_core.provider_apply import ProviderApplyService

    try:
        with catalog_transaction() as conn:
            preview = ProviderApplyService(conn).preview(mapping_id)
        return {"ok": True, "preview": preview.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.post("/provider-mappings/{mapping_id}/apply")
async def provider_mapping_apply(
    mapping_id: str, body: ProviderApplyBody | None = None
):
    """Explicit Apply: active mapping → Execution Source via Phase 3."""
    from catalog_core.provider_apply import ProviderApplyService

    payload = body or ProviderApplyBody()
    try:
        with catalog_transaction() as conn:
            result = ProviderApplyService(conn).apply(
                mapping_id,
                expect_no_current_source=payload.expect_no_current_source,
                expected_current_provider_slug=payload.expected_current_provider_slug,
                expected_current_provider_account_key=payload.expected_current_provider_account_key,
                expected_current_external_service_id=payload.expected_current_external_service_id,
            )
        return {"ok": True, **result.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc


@router.get("/legacy-migration/dry-run")
async def legacy_migration_dry_run():
    """Phase 8C — read-only legacy → Catalog migration plan (no writes)."""
    from catalog_core.legacy_migration import plan_legacy_migration_at_path

    try:
        report = plan_legacy_migration_at_path()
        return {"ok": True, "dry_run": report.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=500,
            detail=f"تعذر قراءة قاعدة البيانات: {exc}",
        ) from exc


@router.get("/legacy-migration/reconciliation")
async def legacy_migration_reconciliation():
    """Phase 8E — read-only post-migration reconciliation (no writes)."""
    from catalog_core.legacy_reconciliation import reconcile_legacy_migration_at_path

    try:
        report = reconcile_legacy_migration_at_path(expect_approved_baseline=True)
        return {"ok": True, "reconciliation": report.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=500,
            detail=f"تعذر قراءة قاعدة البيانات: {exc}",
        ) from exc


@router.get("/storefront/shadow-comparison")
async def storefront_shadow_comparison():
    """Phase 9B.3 — read-only Legacy vs Published Catalog parity audit."""
    from catalog_core.storefront_shadow import compare_storefronts_at_path

    try:
        report = compare_storefronts_at_path()
        return {"ok": True, "shadow": report.to_dict()}
    except CatalogError as exc:
        raise _http_error(exc) from exc
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=500,
            detail=f"تعذر قراءة قاعدة البيانات: {exc}",
        ) from exc
