"""AI Gateway service for Kriya AI.

Provides centralized model routing using OpenRouter's native multi-model
payload (`models: [primary, fallback]`), automatic 429/5xx retry with backoff,
token usage & real-cost ledger recording (`ai_usage_ledger`), and monthly
administrative spend cap checks.

Patient WhatsApp chat messages are NEVER cut off or degraded by the cap.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import httpx

from app.config import settings

logger = logging.getLogger("kriya.ai_gateway")


class SpendCapExceededError(Exception):
    """Raised when an administrative AI feature exceeds the clinic's monthly budget."""
    pass


def _clean_clinic_uuid(raw_id: Optional[str]) -> Optional[str]:
    """Convert raw clinic ID to a valid UUID string or None for unattributed."""
    if not raw_id:
        return None
    cleaned = str(raw_id).strip()
    if cleaned.lower() in ("default", "none", "null", ""):
        return None
    try:
        return str(UUID(cleaned))
    except (ValueError, TypeError, AttributeError):
        return None


async def record_ai_usage(
    clinic_id: Optional[str],
    task_type: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    cost_paise: int,
    is_fallback: bool = False,
    success: bool = True,
    error_message: Optional[str] = None,
) -> None:
    """Record an AI invocation to ai_usage_ledger.

    Safe and non-raising: failures to record (e.g. table not yet migrated)
    log a warning and never propagate.
    """
    cleaned_id = _clean_clinic_uuid(clinic_id)
    try:
        from app.database import supabase, sb

        payload = {
            "clinic_id": cleaned_id,
            "task_type": task_type,
            "provider": provider,
            "model": model or "unknown",
            "prompt_tokens": max(0, int(prompt_tokens or 0)),
            "completion_tokens": max(0, int(completion_tokens or 0)),
            "total_tokens": max(0, int(total_tokens or 0)),
            "cost_paise": max(0, int(cost_paise or 0)),
            "is_fallback": bool(is_fallback),
            "success": bool(success),
            "error_message": (str(error_message).strip())[:300] if error_message else None,
        }
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("ai_usage_ledger").insert(payload))
    except Exception as e:
        logger.warning(f"Could not record AI usage in ai_usage_ledger: {e}")


def record_ai_usage_bg(
    clinic_id: Optional[str],
    task_type: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    cost_paise: int,
    is_fallback: bool = False,
    success: bool = True,
    error_message: Optional[str] = None,
) -> None:
    """Fire-and-forget background wrapper for record_ai_usage via spawn_background_task."""
    try:
        from app.utils.async_tasks import spawn_background_task
        spawn_background_task(
            record_ai_usage(
                clinic_id=clinic_id,
                task_type=task_type,
                provider=provider,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cost_paise=cost_paise,
                is_fallback=is_fallback,
                success=success,
                error_message=error_message,
            ),
            name=f"ai_ledger_{task_type}",
        )
    except RuntimeError:
        # No running event loop in synchronous test or background thread
        pass


async def check_admin_spend_cap(clinic_id: Optional[str]) -> Tuple[bool, int, int]:
    """Check whether a clinic's monthly administrative AI budget is exhausted.

    Returns:
        (is_exceeded, current_spend_paise, budget_paise)

    Admin features (import, details, cleanup, summaries, reply drafts) check
    this before calling external LLMs. Patient WhatsApp chat is never blocked.
    """
    cleaned_id = _clean_clinic_uuid(clinic_id)
    default_budget = getattr(settings, "ai_default_monthly_budget_paise", 50000)

    if not cleaned_id:
        return (False, 0, default_budget)

    from app.database import supabase, sb

    # 1. Read clinic-configured budget
    budget_paise = default_budget
    try:
        clinic_res = await sb(
            supabase.table("clinics").select("config").eq("id", cleaned_id).limit(1)
        )
        if clinic_res.data and clinic_res.data[0].get("config"):
            cfg = clinic_res.data[0]["config"]
            if isinstance(cfg, dict) and "ai_budget_paise" in cfg:
                budget_paise = int(cfg["ai_budget_paise"])
    except Exception as e:
        logger.warning(f"Could not read clinic config for AI budget: {e}")

    # 2. Sum admin spend for the current UTC calendar month in 1,000-row pages
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc).isoformat()
    current_spend_paise = 0
    try:
        PAGE_SIZE = 1000
        offset = 0
        while True:
            ledger_res = await sb(
                supabase.table("ai_usage_ledger")
                .select("cost_paise")
                .eq("clinic_id", cleaned_id)
                .neq("task_type", "patient_chat")
                .gte("created_at", month_start)
                .range(offset, offset + PAGE_SIZE - 1)
            )
            batch = ledger_res.data or []
            current_spend_paise += sum(r.get("cost_paise", 0) for r in batch)
            if len(batch) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
    except Exception as e:
        # If ledger table doesn't exist yet, do not block admin work
        logger.warning(f"Could not query ai_usage_ledger for spend cap check: {e}")
        return (False, 0, budget_paise)

    is_exceeded = current_spend_paise >= budget_paise
    return (is_exceeded, current_spend_paise, budget_paise)


def calculate_cost_paise(
    usage_dict: Dict[str, Any],
    total_tokens: int,
    usd_to_inr_rate: Optional[float] = None,
) -> int:
    """Calculate cost in integer paise from OpenRouter usage dict or token count."""
    rate = usd_to_inr_rate or getattr(settings, "ai_usd_to_inr_rate", 87.0)
    # OpenRouter may provide "total_cost" (in USD)
    cost_usd = usage_dict.get("total_cost") or usage_dict.get("cost")
    if cost_usd is not None:
        try:
            return max(0, int(round(float(cost_usd) * rate * 100)))
        except (ValueError, TypeError):
            pass

    # Fallback estimate: typical DeepSeek/Gemini Flash pricing ~$0.20 per 1M tokens
    # ($0.20 * 87 * 100 paise = 1740 paise / 1M tokens = 0.00174 paise / token)
    if total_tokens > 0:
        return max(1, int(round((total_tokens / 1_000_000.0) * 0.20 * rate * 100)))
    return 0


async def call_ai_gateway(
    messages: List[Dict[str, str]],
    task_type: str,
    clinic_id: Optional[str] = None,
    primary_model: Optional[str] = None,
    fallback_model: Optional[str] = None,
    timeout: Optional[float] = None,
    max_tokens: int = 500,
    temperature: float = 0.2,
    response_format: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Execute completion via OpenRouter with native multi-model fallback and spend tracking.

    Used by administrative features (details, cleanup, import, summaries, staff reply drafts).
    Checks spend cap before calling external LLM.
    """
    # Enforce spend cap for admin features
    if task_type != "patient_chat":
        is_exceeded, current_spend, budget = await check_admin_spend_cap(clinic_id)
        if is_exceeded:
            raise SpendCapExceededError(
                f"Monthly AI budget of ₹{budget/100:.2f} exceeded (spent ₹{current_spend/100:.2f}). "
                "Update budget in Settings or wait until next month."
            )

    active_key = settings.openrouter_api_key
    if not active_key:
        raise ValueError("OPENROUTER_API_KEY is not configured")

    primary = primary_model or settings.openrouter_model or "deepseek/deepseek-chat"
    fallback = fallback_model or getattr(settings, "openrouter_fallback_model", "google/gemini-2.0-flash-001")
    req_timeout = timeout or float(settings.openrouter_timeout or 10)

    # OpenRouter native models fallback list
    payload: Dict[str, Any] = {
        "model": primary,
        "models": [primary, fallback],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = response_format

    headers = {
        "Authorization": f"Bearer {active_key}",
        "HTTP-Referer": "https://kriya.health",
        "X-Title": "Kriya AI Healthcare OS",
        "Content-Type": "application/json",
    }

    # Retry logic: max 2 attempts (req_timeout each, 2s backoff)
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=req_timeout) as client:
                response = await client.post(
                    settings.openrouter_base_url,
                    headers=headers,
                    json=payload,
                )

                if response.status_code == 200:
                    data = response.json()
                    used_model = data.get("model", primary)
                    is_fallback = (used_model != primary)
                    usage = data.get("usage", {})
                    p_tok = usage.get("prompt_tokens", 0)
                    c_tok = usage.get("completion_tokens", 0)
                    tot_tok = usage.get("total_tokens", p_tok + c_tok)
                    cost_paise = calculate_cost_paise(usage, tot_tok)

                    # Record spend to ledger in background
                    record_ai_usage_bg(
                        clinic_id=clinic_id,
                        task_type=task_type,
                        provider="openrouter",
                        model=used_model,
                        prompt_tokens=p_tok,
                        completion_tokens=c_tok,
                        total_tokens=tot_tok,
                        cost_paise=cost_paise,
                        is_fallback=is_fallback,
                        success=True,
                    )
                    return data

                if response.status_code == 429:
                    if attempt < 1:
                        logger.warning(f"OpenRouter 429 on {task_type}. Retrying in 2s...")
                        await asyncio.sleep(2)
                        continue
                    raise RuntimeError("OpenRouter rate limit exceeded (429)")

                if response.status_code in (502, 503, 504):
                    if attempt < 1:
                        logger.warning(f"OpenRouter {response.status_code} on {task_type}. Retrying in 2s...")
                        await asyncio.sleep(2)
                        continue
                    raise RuntimeError(f"OpenRouter service error ({response.status_code})")

                error_text = response.text[:300]
                raise RuntimeError(f"OpenRouter API returned HTTP {response.status_code}: {error_text}")

        except httpx.TimeoutException as te:
            if attempt < 1:
                logger.warning(f"OpenRouter timeout on {task_type}. Retrying in 2s...")
                await asyncio.sleep(2)
                continue
            record_ai_usage_bg(
                clinic_id=clinic_id,
                task_type=task_type,
                provider="openrouter",
                model=primary,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                cost_paise=0,
                success=False,
                error_message=f"Timeout after 2 attempts: {te}",
            )
            raise

        except Exception as e:
            if attempt >= 1 or "rate limit" in str(e).lower() or "service error" in str(e).lower():
                record_ai_usage_bg(
                    clinic_id=clinic_id,
                    task_type=task_type,
                    provider="openrouter",
                    model=primary,
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                    cost_paise=0,
                    success=False,
                    error_message=str(e)[:300],
                )
                raise
            await asyncio.sleep(1)

    raise RuntimeError("OpenRouter completion failed after retry budget.")
