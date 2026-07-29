"""Depiction routes — "this asset shows that product" as a sourced CLAIM.

Not a foreign key.  MFC surfaces the featured release's JAN rather than the
item's, and retailers routinely attach the wrong variant's photo; a FK asserts
truth, whereas a claim records who said so, when, and how confidently, and
lets survivorship rank them.

Reads resolve ``product_redirect`` with COALESCE rather than rewriting
``depiction.product_id``, because rewriting destroys un-merge reversibility.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from ..db import get_db
from ..deps import require_auth_ctx
from ..models import Asset, Depiction, ProductRedirect
from ..schemas import (
    CreateDepictionRequest,
    CreateDepictionResponse,
    DepictionListResponse,
    DepictionSummary,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..policy import AuthCtx

router = APIRouter(tags=["depictions"])


@router.post("/assets/{sha256}/depictions", response_model=CreateDepictionResponse)
def create_depiction(
    sha256: str,
    payload: CreateDepictionRequest,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> CreateDepictionResponse:
    if db.get(Asset, sha256) is None:
        raise HTTPException(status_code=404, detail="not found")
    # A claim with no stable subject handle cannot be deduped.  Refuse it
    # LOUDLY rather than silently merging every such claim into one row.
    if payload.source_native_id is None and payload.subject_product_id is None:
        raise HTTPException(
            status_code=422,
            detail="a depiction needs source_native_id or subject_product_id as its subject",
        )
    if payload.source_id is None and payload.subject_product_id is None:
        raise HTTPException(
            status_code=422,
            detail="a first-party depiction must name subject_product_id",
        )
    as_of = payload.as_of or dt.datetime.now(dt.UTC)
    depiction = Depiction(
        asset_sha256=sha256,
        source_id=payload.source_id,
        source_native_id=payload.source_native_id,
        subject_product_id=payload.subject_product_id,
        product_id=payload.product_id,
        role=payload.role,
        alt_text=payload.alt_text,
        source_url=payload.source_url,
        ruleset_version=payload.ruleset_version,
        conf=payload.conf,
        rank=payload.rank,
        as_of=as_of,
        last_seen_at=as_of,
    )
    db.add(depiction)
    db.commit()
    return CreateDepictionResponse(id=depiction.id)


@router.get("/products/{product_id}/depictions", response_model=DepictionListResponse)
def depictions_for_product(
    product_id: str,
    role: str | None = None,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> DepictionListResponse:
    # Merge-as-redirect, resolved at READ.
    redirect = db.get(ProductRedirect, product_id)
    winner = redirect.winner_product_id if redirect else product_id

    stmt = select(Depiction).where(Depiction.product_id == winner)
    if role:
        stmt = stmt.where(Depiction.role == role)
    # rank is a SOURCE TIER, "lower = more authoritative".
    stmt = stmt.order_by(Depiction.rank.is_(None), Depiction.rank, Depiction.as_of)
    rows = db.execute(stmt).scalars().all()
    return DepictionListResponse(
        results=[
            DepictionSummary(
                id=d.id,
                asset_sha256=d.asset_sha256,
                role=d.role,
                source_id=d.source_id,
                source_native_id=d.source_native_id,
                subject_product_id=d.subject_product_id,
                product_id=d.product_id,
                conf=d.conf,
                rank=d.rank,
            )
            for d in rows
        ]
    )
