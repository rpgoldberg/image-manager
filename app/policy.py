"""Access policy: who may SEE an asset.

Distinct from :mod:`app.rights`, which decides what we may DO to it.  v1
conflated them by hanging ``visibility`` off ``ImageVersion`` — rights are a
property of where the bytes came from, not of a rendering of them.  Here
visibility lives on the grant (``user_asset_link``) or the album item, and age
rating lives on the bytes (``asset.content_rating``).
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import rating_rank


@dataclass
class AuthCtx:
    subject: str
    tenant_id: str | None
    is_service: bool
    scopes: list[str]
    safe_mode: bool = False


def can_view(
    ctx: AuthCtx | None,
    visibility: str,
    owner_tenant_id: str | None = None,
    content_rating: str = "unknown",
    share_threshold: str | None = None,
) -> bool:
    """Visibility first, then the age gate.

    ``content_rating`` and ``share_threshold`` are both
    :data:`~app.models.CONTENT_RATING_VALUES` members now, so they are
    comparable again: v1 stored an integer age on the *version* and an integer
    threshold on the album, and the restructure would have left the two
    unrelated.

    The age gate applies only in a gated context — a share link, or a caller
    asking for safe mode.  ``unknown`` ranks MOST restrictive, so an unrated
    asset is withheld from both rather than leaking on the default.
    """
    if visibility == "public":
        allowed = True
    elif visibility == "catalog":
        # Catalog requires a service token scope.
        allowed = bool(ctx and ctx.is_service and ("assets:read" in ctx.scopes))
    elif visibility == "tenant":
        allowed = bool(
            ctx and ctx.tenant_id and owner_tenant_id and ctx.tenant_id == owner_tenant_id
        )
    else:
        # private: requires a grant, which is enforced at query time.
        allowed = True

    if not allowed:
        return False

    if share_threshold is not None:
        return rating_rank(content_rating) <= rating_rank(share_threshold)
    if ctx is not None and ctx.safe_mode:
        return rating_rank(content_rating) <= rating_rank("all_ages")
    return True
