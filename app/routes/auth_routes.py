from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import auth
from ..config import Settings, get_settings
from ..db import get_db
from ..schemas import DevTokenRequest, DevTokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/dev-token", response_model=DevTokenResponse)
def issue_dev_token(
    payload: DevTokenRequest,
    settings: Settings = Depends(get_settings),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> DevTokenResponse:
    if not settings.allow_dev_tokens:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="dev token issuance disabled"
        )

    try:
        token = auth.issue_dev_token(
            db, settings, user_id=payload.user_id, tenant_id=payload.tenant_id, aud=payload.aud
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return DevTokenResponse(token=token)
