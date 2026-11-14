"""Diagnostic OS public routes (DX-46, SECURITY.md §6): report verification and share links.

No session, no admin auth import. Every route is rate limited per IP. The verify page
shows no values, no phone and no full name; a share link asks for the last 4 digits of
the registered phone (5 attempts, then locked), re-checks the delivery policy at unlock
and streams the stored PDF with Cache-Control: no-store. Zero-LLM.
"""

import asyncio
import html
import logging

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.services import dx_reports
from app.utils.security import PersistentRateLimiter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/public/dx", tags=["Diagnostic OS public"])

_verify_limiter = PersistentRateLimiter(max_attempts=30, window_seconds=60)
_share_limiter = PersistentRateLimiter(max_attempts=10, window_seconds=60)
CODE = Path(..., pattern=r"^[A-Z2-7]{10}$")
TOKEN = Path(..., pattern=r"^[A-Za-z0-9_-]{16,64}$")
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
           "X-Frame-Options": "DENY"}
PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'"


class UnlockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone_last4: str = Field(..., pattern=r"^[0-9]{4}$")


async def _limit(request: Request, limiter: PersistentRateLimiter, kind: str) -> None:
    ip = request.client.host if request.client else "unknown"
    if await asyncio.to_thread(limiter.check_and_record, f"dx-{kind}:{ip}"):
        raise HTTPException(status_code=429, detail="Too many requests. Please wait a minute.", headers={"Retry-After": "60"})


@router.get("/verify/{code}")
async def verify_report(request: Request, code: str = CODE):
    await _limit(request, _verify_limiter, "verify")
    out = await dx_reports.public_verify(code)
    if not out:
        raise HTTPException(status_code=404, detail="No report has this verification code.")
    from fastapi.responses import JSONResponse
    return JSONResponse(out, headers=HEADERS)


def _page(body: str) -> HTMLResponse:
    doc = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
           "content='width=device-width,initial-scale=1'><title>Lab report</title><style>"
           "body{font-family:system-ui,sans-serif;max-width:420px;margin:48px auto;padding:0 16px;color:#1f2933}"
           "input{font-size:20px;letter-spacing:6px;width:7em;padding:8px}button{font-size:16px;padding:9px 18px}"
           "</style></head><body>" + body + "</body></html>")
    return HTMLResponse(doc, headers={**HEADERS, "Content-Security-Policy": PAGE_CSP})


@router.get("/r/{token}", response_class=HTMLResponse)
async def share_page(request: Request, token: str = TOKEN):
    """Asks for the last 4 digits of the registered phone; the form posts to /unlock (no script needed)."""
    await _limit(request, _share_limiter, "share")
    t = html.escape(token)
    return _page("<h2>Your lab report</h2><p>Enter the last 4 digits of the mobile number registered at the lab.</p>"
                 f"<form method='post' action='/public/dx/r/{t}/unlock'><input name='phone_last4' inputmode='numeric' "
                 "pattern='[0-9]{4}' maxlength='4' required autocomplete='off'> <button type='submit'>Open report</button>"
                 "</form><p><small>The link expires and locks after 5 wrong tries.</small></p>")


_MESSAGES = {"not_found": (404, "This link is not valid."), "expired": (410, "This link has expired. Ask the lab for a new one."),
             "locked": (423, "Too many wrong tries. Ask the lab for a new link."),
             "wrong": (403, "Those digits do not match the registered number."),
             "held": (409, "This report is not available right now. Please contact the lab.")}


@router.post("/r/{token}/unlock")
async def unlock_report(request: Request, token: str = TOKEN):
    await _limit(request, _share_limiter, "unlock")
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        body = UnlockIn.model_validate(await request.json())
    else:                                                     # the HTML form
        form = await request.form()
        try:
            body = UnlockIn(phone_last4=str(form.get("phone_last4") or ""))
        except ValueError:
            raise HTTPException(status_code=422, detail="Enter 4 digits.")
    state, pdf = await dx_reports.public_unlock(token, body.phone_last4)
    if state != "ok":
        code, message = _MESSAGES.get(state, _MESSAGES["not_found"])
        logger.info(f"DX_SHARE_UNLOCK state={state}")
        raise HTTPException(status_code=code, detail={"code": state, "message": message})
    return Response(pdf, media_type="application/pdf",
                    headers={**HEADERS, "Content-Disposition": 'inline; filename="lab-report.pdf"'})
