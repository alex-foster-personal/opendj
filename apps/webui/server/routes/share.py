"""Mint the share cookie so a friend URL can carry ``?share=`` once."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from ..share_gate import SHARE_COOKIE, share_token

router = APIRouter(tags=["share"])


@router.get("/share/session")
def share_session(
    request: Request,
    share: str = Query(default=""),
    next: str = Query(default="/"),
) -> RedirectResponse:
    """Set the share cookie when the query token matches, then redirect."""
    token = share_token()
    if not token:
        raise HTTPException(
            status_code=404,
            detail={"code": "SHARE_DISABLED", "message": "no share token configured"},
        )
    if not share or not hmac.compare_digest(share, token):
        raise HTTPException(
            status_code=401,
            detail={"code": "SHARE_UNAUTHORIZED", "message": "share token missing or wrong"},
        )
    if not next.startswith("/"):
        raise HTTPException(
            status_code=400,
            detail={"code": "SHARE_BAD_NEXT", "message": "next must be a relative path"},
        )
    response = RedirectResponse(url=next, status_code=303)
    response.set_cookie(
        SHARE_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response
