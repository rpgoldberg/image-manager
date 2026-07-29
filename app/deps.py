import logging
import uuid

from fastapi import Depends, Header, HTTPException, Request, status

from .auth import decode_token
from .config import get_settings
from .policy import AuthCtx

logger = logging.getLogger(__name__)


def get_request_id(x_request_id: str | None = Header(default=None)) -> str:
    # Generate or propagate an x-request-id
    rid = x_request_id or str(uuid.uuid4())
    return rid


def get_auth_ctx(request: Request) -> AuthCtx | None:
    settings = get_settings()
    auth_header = request.headers.get("authorization")
    safe_mode = request.headers.get("x-safe-mode") == "1"
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header.split(" ", 1)[1]
    payload = decode_token(settings, token)
    if not payload:
        return None
    sub = str(payload.get("sub"))
    ten = payload.get("ten")
    scopes = payload.get("scopes") or []
    is_service = sub.startswith("service:")
    return AuthCtx(
        subject=sub, tenant_id=ten, is_service=is_service, scopes=list(scopes), safe_mode=safe_mode
    )


def require_auth_ctx(ctx: AuthCtx | None = Depends(get_auth_ctx)) -> AuthCtx:  # noqa: B008
    if ctx is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    return ctx
