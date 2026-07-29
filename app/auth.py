from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING, Any

from jose import JWTError, jwt  # type: ignore[import-untyped]

from .models import ServiceClient

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from .config import Settings

logger = logging.getLogger(__name__)


def create_token(
    settings: Settings,
    *,
    subject: str,
    tenant_id: str | None = None,
    scopes: list[str] | None = None,
    audience: str | None = None,
    ttl_minutes: int | None = None,
) -> str:
    now = dt.datetime.now(dt.UTC)
    exp = now + dt.timedelta(minutes=ttl_minutes or settings.token_exp_minutes)
    payload: dict[str, Any] = {
        "sub": subject,
        "exp": int(exp.timestamp()),
        "iat": int(now.timestamp()),
    }
    if tenant_id:
        payload["ten"] = tenant_id
    if scopes:
        payload["scopes"] = scopes
    if audience:
        payload["aud"] = audience
    token: str = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token


def decode_token(settings: Settings, token: str) -> dict[str, Any] | None:
    try:
        claims: dict[str, Any] = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except JWTError as e:
        logger.warning("jwt_decode_failed", extra={"error": str(e)})
        return None
    return claims


def issue_dev_token(
    db: Session,
    settings: Settings,
    *,
    user_id: str | None = None,
    tenant_id: str | None = None,
    aud: str | None = None,
) -> str:
    if aud and aud.startswith("service:"):
        # service token with scopes looked up from DB
        name = aud.split(":", 1)[1]
        svc = db.query(ServiceClient).filter(ServiceClient.name == name).one_or_none()
        scopes = []
        if svc and svc.scopes:
            scopes = [s.strip() for s in svc.scopes.split(" ") if s.strip()]
        return create_token(settings, subject=f"service:{name}", audience=aud, scopes=scopes)
    # user token
    if not user_id:
        raise ValueError("user_id required for user tokens")
    return create_token(settings, subject=user_id, tenant_id=tenant_id, scopes=[])
