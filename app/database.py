"""Database module for Supabase integration (Multi-Tenant Scoped)."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date as dt_date, timedelta, timezone
import logging
import time
from typing import Optional

import httpx
from supabase import create_client, Client

from app.config import settings

logger = logging.getLogger(__name__)

# TTL Caches for static metadata (doctor cache reduced to 60s for fast admin->bot sync)
_doctor_cache: dict[str, dict] = {}
_holiday_cache: dict[str, dict] = {}
DOCTOR_CACHE_TTL_SECONDS = 60
HOLIDAY_CACHE_TTL_SECONDS = 300


def invalidate_holiday_cache(clinic_id: Optional[str] = None, holiday_date: Optional[str] = None) -> None:
    """Evict cached holiday lookups after an admin adds or removes a holiday.

    get_available_slots caches the RESULT of the hospital_holidays lookup,
    including the empty list meaning "this day is not a holiday". Without this,
    declaring a holiday in the admin panel left the bot offering slots on that
    day — and accepting bookings for it — for up to HOLIDAY_CACHE_TTL_SECONDS.
    Deleting one had the mirror problem: the clinic stayed closed to patients
    for the same window after it was reopened.
    """
    if clinic_id and holiday_date:
        _holiday_cache.pop(f"{clinic_id}:{holiday_date}", None)
    elif clinic_id:
        for k in [k for k in _holiday_cache if k.startswith(f"{clinic_id}:")]:
            _holiday_cache.pop(k, None)
    else:
        _holiday_cache.clear()


def invalidate_doctor_cache(clinic_id: Optional[str] = None, doctor_name: Optional[str] = None) -> None:
    """Evict doctor cache entries. Called immediately upon admin mutations."""
    if doctor_name and clinic_id:
        _doctor_cache.pop(f"{clinic_id}:{doctor_name}", None)
    elif clinic_id:
        keys_to_remove = [k for k in _doctor_cache if k.startswith(f"{clinic_id}:")]
        for k in keys_to_remove:
            _doctor_cache.pop(k, None)
    else:
        _doctor_cache.clear()


# Initialize Supabase client with fallback for zero-downtime boots
_sb_url = settings.supabase_url if (settings.supabase_url and settings.supabase_url.startswith("http")) else "https://placeholder.supabase.co"
_sb_key = settings.supabase_service_role_key or "placeholder-key"
# Bound every PostgREST/storage call (T5.1 / KA-P1-03).
#
# Since sb() runs queries on _DB_EXECUTOR, a call that never returns holds a
# worker thread forever. Without a timeout, a degraded Supabase does not slow
# the service down — it permanently retires the pool one thread at a time until
# no query can run at all, and the failure looks like a hang rather than an
# error. A bounded call surfaces as an exception that the existing fail-closed
# handling already knows what to do with.
#
# HTTP/1.1 is forced deliberately: the httpx default HTTP/2 multiplexes all
# requests over a single TCP connection. If one request times out, httpx tears
# down the shared H2 connection and every in-flight request multiplexed on it
# fails with ReadTimeout simultaneously — visible in production as a cascade
# where every DB operation fails at the exact same instant. With HTTP/1.1 each
# request uses its own connection from the pool, so a single slow query cannot
# poison other requests.
_db_timeout = httpx.Timeout(
    settings.db_query_timeout_seconds,
    connect=10.0,  # allow extra time for TCP+TLS handshake under cold-start
)
_db_transport = httpx.HTTPTransport(
    retries=1,         # one transparent retry on connection-level errors
    http2=False,       # force HTTP/1.1 — see note above
)
_db_httpx_client = httpx.Client(
    timeout=_db_timeout,
    transport=_db_transport,
)

try:
    # create_client() (the sync client) takes SyncClientOptions.
    from supabase.lib.client_options import SyncClientOptions

    _sb_options = SyncClientOptions(
        postgrest_client_timeout=settings.db_query_timeout_seconds,
        storage_client_timeout=settings.db_query_timeout_seconds,
    )
    supabase: Client = create_client(_sb_url, _sb_key, options=_sb_options)

    # Patch the PostgREST session to use our HTTP/1.1 transport.
    # supabase-py exposes the underlying httpx session on the postgrest client.
    if hasattr(supabase, "postgrest") and hasattr(supabase.postgrest, "_session"):
        supabase.postgrest._session = _db_httpx_client
        logger.info(
            "Supabase PostgREST client patched: HTTP/1.1 forced, "
            f"timeout={settings.db_query_timeout_seconds}s, "
            f"connect_timeout=10s, retries=1"
        )
except Exception as _opt_err:  # pragma: no cover - defensive
    logger.warning(
        f"Could not apply Supabase client timeouts ({_opt_err}); falling back to "
        f"library defaults. A hung query can occupy a DB worker thread."
    )
    supabase: Client = create_client(_sb_url, _sb_key)

# Dedicated worker pool for blocking PostgREST calls (T5.1 / KA-P1-03).
#
# Module-level and therefore loop-independent: it survives the event loop being
# replaced, which matters both for pytest-asyncio (a fresh loop per test) and
# for any future code that runs a second loop.
#
# Sizing is the per-process database concurrency ceiling. Three limits sit
# behind it, and the smallest wins:
#
#   1. these worker threads                    (db_thread_pool_size, 16)
#   2. the shared httpx connection pool inside the supabase client
#      (max_connections defaults to 100), which all threads contend for
#   3. PostgREST's own database pool on the Supabase side
#
# 16 x 2 processes = up to 32 concurrent HTTP calls to PostgREST, which fits a
# Supabase Micro/Small connection budget. That is requests, not Postgres
# connections — PostgREST multiplexes them onto its own pool — but it is still
# the number to check against the deployment's plan limits.
#
# This was 64 until the cascading-timeout incident (AUDIT-P1-3). 64 x 4
# processes overshot what Micro would accept, and the queue simply moved from
# this limiter to the connection pool, converting a bounded wait into a
# timeout. Raising it again is UNVERIFIED under real load: T8.1 must show it
# improves throughput rather than just relocating the queue.
_DB_EXECUTOR = ThreadPoolExecutor(
    max_workers=getattr(settings, "db_thread_pool_size", 16),
    thread_name_prefix="kriya-db",
)


#: Methods safe to replay after "Server disconnected". PostgREST PATCH and
#: DELETE assign literal values to rows selected by filter, so replaying one
#: reaches the same end state. POST is excluded: an insert cannot distinguish
#: "never sent" from "committed, then the connection dropped", and replaying it
#: would duplicate a row.
_RETRYABLE_METHODS = frozenset({"GET", "HEAD", "PATCH", "DELETE"})


async def sb(builder):
    """Execute a PostgREST query off the event loop (KA-P1-03).

    supabase-py 2.x's `create_client()` returns the SYNCHRONOUS `Client`; the
    async variant is `create_async_client()`. Every `.execute()` is therefore a
    blocking httpx request. Calling one directly inside an `async def` freezes
    the whole event loop for the duration of the round-trip — so within each of
    the four production processes (2 Render instances x 2 uvicorn workers),
    FastAPI's concurrency was nullified and requests served strictly one at a
    time.

    It compounds: the webhook hands full message processing to
    BackgroundTasks, which run on that same loop after the response is sent, so
    a booking turn (a Groq call plus ~15 sequential blocking round-trips) froze
    the loop that must acknowledge the next Meta webhook inside 20 seconds.
    Under load Meta retries, and the retries multiply the load.

    Usage — build the query exactly as before, then await it:

        res = await sb(supabase.table("patients").select("*").eq("clinic_id", c))

    Only the execution moves to a worker thread. The builder is constructed
    synchronously (no I/O), and the return value and exception semantics are
    identical to `.execute()`, which is what makes the conversion of ~390 call
    sites mechanical and individually revertible.

    Deliberately NOT a switch to `create_async_client()`: that would change the
    semantics of every call site at once, and this codebase has no test that
    blocks on real I/O, so such a regression would be invisible to the suite.

    Uses a dedicated executor rather than `asyncio.to_thread`, for two reasons:

    1. `asyncio.to_thread` dispatches to the RUNNING LOOP's default executor.
       Under pytest-asyncio every test gets a fresh event loop, so a new
       default executor is created and abandoned per test — which made the
       suite fail nondeterministically late in a long run.
    2. The loop's default executor is shared with Starlette's `run_in_threadpool`
       (sync endpoints, `UploadFile` reads). Database work having its own
       bounded pool means a burst of queries cannot starve request handling,
       and the size is a knob that means something.

    Retries once on a stale pooled connection — see _RETRYABLE_METHODS.
    """
    # Runtime Tenant Scoping Inspection (Defense-in-depth)
    try:
        req = getattr(builder, "request", None)
        if req and hasattr(req, "path"):
            path_str = str(req.path or "")
            table_name = path_str.rstrip("/").split("/")[-1].split("?")[0]
            if table_name in TENANT_OWNED_TABLES and not getattr(builder, "_allow_unscoped", False):
                params_str = str(getattr(req, "params", "") or "")
                json_payload = getattr(req, "json", None)
                has_clinic = "clinic_id" in params_str
                has_pk = "id=eq." in params_str
                has_payload_clinic = False
                if isinstance(json_payload, dict):
                    has_payload_clinic = "clinic_id" in json_payload
                elif isinstance(json_payload, list) and len(json_payload) > 0:
                    has_payload_clinic = all("clinic_id" in row for row in json_payload if isinstance(row, dict))
                if not (has_clinic or has_pk or has_payload_clinic):
                    logger.warning(
                        f"QUERY_TENANT_SCOPE_AUDIT: Query on tenant table '{table_name}' without explicit clinic_id "
                        f"params='{params_str[:120]}'"
                    )
    except Exception:
        pass

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_DB_EXECUTOR, builder.execute)
    except httpx.RemoteProtocolError as exc:
        # "Server disconnected without sending a response". httpx keeps pooled
        # keep-alive connections; PostgREST closes idle ones on its side, so a
        # connection can already be dead when we pick it up and the request
        # fails before reaching the server. One retry gets a fresh connection.
        method = getattr(
            getattr(builder, "request", None), "http_method", None
        )
        method = str(getattr(method, "value", method) or "").upper()
        if method not in _RETRYABLE_METHODS:
            raise
        logger.warning(
            "Supabase connection was stale on %s (%s); retrying once", method, exc
        )
        return await loop.run_in_executor(_DB_EXECUTOR, builder.execute)


class TenantIsolationError(RuntimeError):
    """Raised when a tenant-owned table is queried without a valid clinic scope."""


#: Tables whose rows belong to exactly one clinic. Reading these without a
#: clinic_id is always a bug — the application connects as Supabase
#: `service_role`, which holds BYPASSRLS, so the database will happily return
#: every tenant's rows. This set is the single source of truth.
# The canonical tenant-table list and scope predicate live in app/tenancy.py —
# a module with no imports, so the tests that fake `app.database` in sys.modules
# cannot turn either of them into a MagicMock. Re-exported here because ~30 call
# sites already import them from this module.
from app.tenancy import TENANT_OWNED_TABLES, is_valid_clinic_scope  # noqa: F401,E402


def scoped_query(
    table_name: str,
    clinic_id: Optional[str] = None,
    select_fields: str = "*",
    allow_unscoped: bool = False,
):
    """Build a Supabase select query that is scoped by clinic_id, or refuses to build.

    Fails closed. For any table in TENANT_OWNED_TABLES a missing, empty, or
    sentinel clinic_id raises TenantIsolationError rather than returning an
    unscoped query. Previously this silently returned every tenant's rows, which
    made a forgotten clinic_id indistinguishable from a deliberate global read.

    RLS is not a backstop here: the app connects as `service_role` (BYPASSRLS),
    so this guard is the enforcement boundary, not a second layer.

    Args:
        table_name: Database table name (e.g. 'appointments', 'patients').
        clinic_id: Target tenant clinic ID.
        select_fields: SQL fields to select (default: '*').
        allow_unscoped: Deliberate cross-tenant read (platform-owner reporting,
            tenant-resolution bootstrap). Must be passed explicitly at the call
            site so it is greppable in review.

    Raises:
        TenantIsolationError: tenant-owned table queried without a valid scope.
    """
    scoped = is_valid_clinic_scope(clinic_id)

    if table_name in TENANT_OWNED_TABLES and not scoped and not allow_unscoped:
        raise TenantIsolationError(
            f"Refusing to build an unscoped query on tenant-owned table "
            f"'{table_name}': clinic_id={clinic_id!r} is not a valid scope. "
            f"If this is a deliberate cross-tenant read (platform reporting, "
            f"tenant-resolution bootstrap), pass allow_unscoped=True."
        )

    q = supabase.table(table_name).select(select_fields)
    if scoped:
        q = q.eq("clinic_id", clinic_id)
    return q



async def get_patient_by_phone(clinic_id: str, phone: str) -> Optional[dict]:
    """Get patient by phone number and clinic_id."""
    try:
        result = (
            await sb(scoped_query("patients", clinic_id)
            .eq("phone", phone))
        )
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"Error getting patient: {e}")
        return None


async def create_patient(
    clinic_id: str,
    phone: str,
    name: Optional[str] = None,
    language: Optional[str] = None,
) -> dict:
    """Create a new patient in a race-safe manner."""
    if not is_valid_clinic_scope(clinic_id):
        raise TenantIsolationError(f"Refusing create_patient on invalid clinic_id: {clinic_id!r}")

    try:
        from datetime import datetime, timezone

        now_iso = datetime.now(timezone.utc).isoformat()
        data = {
            "clinic_id": clinic_id,
            "phone": phone,
            "name": name,
            "language": language,
            "opted_in": True,
            "opted_in_at": now_iso,
            "last_seen_at": now_iso,
        }
        # unscoped: insert_scoped_by_payload
        result = await sb(supabase.table("patients").insert(data))
        return result.data[0]
    except Exception as e:
        error_str = str(e).lower()
        if "unique" in error_str or "duplicate" in error_str or "23505" in error_str:
            existing = await get_patient_by_phone(clinic_id, phone)
            if existing:
                return existing
        logger.error(f"Error creating patient: {e}")
        raise


async def update_patient(clinic_id: str, phone: str, updates: dict) -> bool:
    """Update patient data."""
    if not is_valid_clinic_scope(clinic_id):
        raise TenantIsolationError(f"Refusing update_patient on invalid clinic_id: {clinic_id!r}")

    try:
        await sb(supabase.table("patients").update(updates).eq("clinic_id", clinic_id).eq(
            "phone", phone
        ))
        return True
    except Exception as e:
        logger.error(f"Error updating patient: {e}")
        return False


async def get_genuine_patients(clinic_id: str) -> list[dict]:
    """Get list of genuine patients with clinical engagement.
    
    Excludes transient, unengaged WhatsApp contacts (visit_count == 0, name is null/placeholder,
    and no appointments, lab reports, or prescriptions).
    """
    if not is_valid_clinic_scope(clinic_id):
        raise TenantIsolationError(f"Refusing get_genuine_patients on invalid clinic_id: {clinic_id!r}")

    try:
        import asyncio
        p_task = sb(
            supabase.table("patients")
            .select("*")
            .eq("clinic_id", clinic_id)
            .order("phone")
            .limit(2000)
        )
        a_task = sb(
            supabase.table("appointments")
            .select("patient_phone")
            .eq("clinic_id", clinic_id)
            .limit(2000)
        )
        r_task = sb(
            supabase.table("lab_reports")
            .select("patient_phone")
            .eq("clinic_id", clinic_id)
            .limit(2000)
        )
        pr_task = sb(
            supabase.table("prescriptions")
            .select("patient_phone")
            .eq("clinic_id", clinic_id)
            .limit(2000)
        )

        p_res, a_res, r_res, pr_res = await asyncio.gather(
            p_task, a_task, r_task, pr_task
        )
        raw_patients = p_res.data or []
        appt_phones = {x["patient_phone"] for x in (a_res.data or []) if x.get("patient_phone")}
        lab_phones = {x["patient_phone"] for x in (r_res.data or []) if x.get("patient_phone")}
        presc_phones = {x["patient_phone"] for x in (pr_res.data or []) if x.get("patient_phone")}
        clinical_phones = appt_phones | lab_phones | presc_phones

        disallowed_names = {
            "our services", "services", "book appointment", "cancel appointment",
            "menu", "main menu", "check reports", "lab test", "test", "admin", "user"
        }

        def is_genuine(p: dict) -> bool:
            if (p.get("visit_count") or 0) > 0:
                return True
            if p.get("phone") in clinical_phones:
                return True
            name = (p.get("name") or "").strip().lower()
            if name and name not in disallowed_names and len(name) >= 3:
                return True
            return False

        return [p for p in raw_patients if is_genuine(p)]
    except Exception as e:
        logger.error(f"Error getting genuine patients for clinic {clinic_id}: {e}")
        # Fallback to basic query if any subquery fails
        try:
            res = await sb(supabase.table("patients").select("*").eq("clinic_id", clinic_id).limit(2000))
            return [p for p in (res.data or []) if (p.get("visit_count") or 0) > 0 or p.get("name")]
        except Exception:
            return []


async def get_conversation(clinic_id: str, phone: str) -> Optional[dict]:
    """Get conversation session for phone."""
    try:
        result = (
            await sb(scoped_query("conversations", clinic_id)
            .eq("phone", phone))
        )
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"Error getting conversation: {e}")
        return None


async def create_conversation(clinic_id: str, phone: str) -> dict:
    """Create a new conversation session."""
    try:
        from datetime import datetime, timedelta, timezone

        now_dt = datetime.now(timezone.utc)
        data = {
            "clinic_id": clinic_id,
            "phone": phone,
            "state": "idle",
            "context": {},
            "session_expires_at": (
                now_dt + timedelta(hours=24)
            ).isoformat(),
            "last_message_at": now_dt.isoformat(),
        }
        # unscoped: insert_scoped_by_payload
        result = await sb(supabase.table("conversations").insert(data))
        return result.data[0]
    except Exception as e:
        error_str = str(e).lower()
        if "unique" in error_str or "duplicate" in error_str or "23505" in error_str:
            existing = await get_conversation(clinic_id, phone)
            if existing:
                return existing
        logger.error(f"Error creating conversation: {e}")
        raise


async def update_conversation(clinic_id: str, phone: str, updates: dict) -> bool:
    """Update conversation session."""
    try:
        from datetime import datetime, timezone

        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        await sb(supabase.table("conversations").update(updates).eq("clinic_id", clinic_id).eq(
            "phone", phone
        ))
        return True
    except Exception as e:
        logger.error(f"Error updating conversation: {e}")
        return False


async def get_or_create_conversation(clinic_id: str, phone: str) -> dict:
    """Get existing conversation or create new one in a race-safe manner."""
    conv = await get_conversation(clinic_id, phone)
    if conv:
        return conv
    try:
        return await create_conversation(clinic_id, phone)
    except Exception:
        # If another concurrent request created the record, re-fetch it
        conv = await get_conversation(clinic_id, phone)
        if conv:
            return conv
        raise


async def get_doctors(
    clinic_id: str,
    department: Optional[str] = None,
    active_only: bool = True,
    branch_id: Optional[str] = None,
) -> list:
    """Get doctors, optionally filtered by department and/or branch.

    When branch_id is provided, only returns doctors assigned to that branch
    via the doctor_branches junction table.
    """
    try:
        if branch_id:
            # Branch-scoped: join through doctor_branches
            return await get_doctors_at_branch(
                clinic_id, branch_id, department, active_only
            )

        query = scoped_query("doctors", clinic_id)

        if department:
            query = query.eq("department", department)
        if active_only:
            query = query.eq("is_active", True)

        result = await sb(query)
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting doctors: {e}")
        return []


async def get_lab_tests(
    clinic_id: str, branch_id: Optional[str] = None, active_only: bool = True
) -> list:
    """Get a clinic's lab test catalog, optionally filtered by branch.

    A test with branch_id=NULL is clinic-wide and is included regardless of
    the branch_id filter (mirrors the catalog's "unset = all branches" rule).
    """
    try:
        query = scoped_query("lab_tests", clinic_id)
        if active_only:
            query = query.eq("is_active", True)
        result = await sb(query.order("name"))
        tests = result.data or []
        if branch_id:
            tests = [
                t for t in tests if not t.get("branch_id") or t["branch_id"] == branch_id
            ]
        return tests
    except Exception as e:
        logger.error(f"Error getting lab tests: {e}")
        return []


async def get_lab_test_by_id(clinic_id: str, lab_test_id: str) -> Optional[dict]:
    """Get a single active lab test by id, scoped to the clinic."""
    try:
        result = (
            await sb(scoped_query("lab_tests", clinic_id)
            .eq("id", lab_test_id)
            .eq("is_active", True))
        )
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"Error getting lab test {lab_test_id}: {e}")
        return None


async def get_lab_collection_window(clinic: dict, branch_id: Optional[str] = None) -> dict:
    """Resolve the sample collection window for a booking.

    Branch-level config takes priority; falls back to clinic-level config
    for single-location clinics; falls back to a hardcoded default if
    neither is configured.
    """
    default = {"start": "07:00", "end": "11:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat"}
    try:
        if branch_id:
            result = (
                # unscoped: unique_row_key
                await sb(supabase.table("branches").select("config").eq("id", branch_id))
            )
            if result.data:
                window = (result.data[0].get("config") or {}).get("lab_collection")
                if window:
                    return window
        window = (clinic.get("config") or {}).get("lab_collection")
        return window or default
    except Exception as e:
        logger.error(f"Error getting lab collection window: {e}")
        return default


def format_collection_window(window: dict) -> str:
    """Human-readable collection-window text for the WhatsApp booking flow.

    Appends a Sunday-specific note only when the clinic both operates on
    Sunday and has configured different Sunday hours — otherwise identical
    to the flat start-end string clinics have always seen.
    """
    base = f"{window.get('start', '07:00')} - {window.get('end', '11:00')}"
    sunday_start = window.get("sunday_start")
    sunday_end = window.get("sunday_end")
    if sunday_start and sunday_end:
        days = {d.strip() for d in window.get("days", "").split(",") if d.strip()}
        if "Sun" in days:
            return f"{base} (Sun: {sunday_start} - {sunday_end})"
    return base


async def get_doctors_at_branch(
    clinic_id: str,
    branch_id: str,
    department: Optional[str] = None,
    active_only: bool = True,
) -> list:
    """Get doctors assigned to a specific branch via doctor_branches junction.

    Returns doctor dicts enriched with 'branch_session' field indicating
    which session ('morning', 'evening', 'both') applies at this branch.
    """
    try:
        # Get doctor_ids assigned to this branch
        db_result = (
            await sb(supabase.table("doctor_branches")
            .select("doctor_id, session")
            .eq("branch_id", branch_id))
        )

        if not db_result.data:
            return []

        # Build lookup: {doctor_id: session}
        doctor_session_map = {
            row["doctor_id"]: row["session"] for row in db_result.data
        }
        doctor_ids = list(doctor_session_map.keys())

        # Fetch doctor details
        query = (
            supabase.table("doctors")
            .select("*")
            .eq("clinic_id", clinic_id)
            .in_("id", doctor_ids)
        )

        if department:
            query = query.eq("department", department)
        if active_only:
            query = query.eq("is_active", True)

        result = await sb(query)
        doctors = result.data or []

        # Enrich with branch_session
        for doc in doctors:
            doc["branch_session"] = doctor_session_map.get(doc["id"], "both")

        return doctors
    except Exception as e:
        logger.error(f"Error getting doctors at branch {branch_id}: {e}")
        return []


async def get_doctor_by_name(clinic_id: str, name: str) -> Optional[dict]:
    """Get doctor by name with TTL cache."""
    cache_key = f"{clinic_id}:{name}"
    cached = _doctor_cache.get(cache_key)
    if cached and (time.time() - cached.get("cached_at", 0) < DOCTOR_CACHE_TTL_SECONDS):
        return cached.get("data")

    try:
        result = (
            await sb(scoped_query("doctors", clinic_id)
            .eq("name", name))
        )
        if result.data:
            doc = result.data[0]
            _doctor_cache[cache_key] = {"data": doc, "cached_at": time.time()}
            return doc
        return None
    except Exception as e:
        logger.error(f"Error getting doctor: {e}")
        return None


async def get_available_slots(
    clinic_id: str,
    doctor_name: str,
    date_str: str,
    branch_id: Optional[str] = None,
    branch_session: Optional[str] = None,
) -> tuple[list, Optional[str]]:
    """Get available slots for a doctor on a specific date using parallel queries & metadata caching."""
    from datetime import datetime, date as dt_date, timedelta

    if not clinic_id:
        raise ValueError("clinic_id is required to query available slots")

    try:
        check_date = datetime.strptime(date_str, "%Y-%m-%d").date()

        def _sync_fetch_holiday():
            cache_key = f"{clinic_id}:{date_str}"
            cached = _holiday_cache.get(cache_key)
            if cached and (time.time() - cached.get("cached_at", 0) < HOLIDAY_CACHE_TTL_SECONDS):
                return cached.get("data")

            res = (
                supabase.table("hospital_holidays")
                .select("name")
                .eq("clinic_id", clinic_id)
                .eq("holiday_date", date_str)
                .execute()
            )
            data = res.data or []
            _holiday_cache[cache_key] = {"data": data, "cached_at": time.time()}
            return data

        def _sync_fetch_leave():
            res = (
                supabase.table("doctor_leaves")
                .select("leave_type")
                .eq("clinic_id", clinic_id)
                .eq("doctor_name", doctor_name)
                .eq("leave_date", date_str)
                .execute()
            )
            return res.data or []

        def _sync_fetch_booked():
            res = (
                supabase.table("appointments")
                .select("appointment_time, status, hold_expires_at, created_at")
                .eq("clinic_id", clinic_id)
                .eq("doctor_name", doctor_name)
                .eq("appointment_date", date_str)
                .in_("status", ["confirmed", "pending_payment"])
                .execute()
            )
            rows = res.data or []
            now_utc = datetime.now(timezone.utc)
            booked = []
            for r in rows:
                status = r.get("status")
                if status in ("confirmed", None):
                    booked.append(r)
                elif status == "pending_payment":
                    hold_exp = r.get("hold_expires_at")
                    if hold_exp:
                        try:
                            exp_dt = datetime.fromisoformat(hold_exp.replace("Z", "+00:00"))
                            if exp_dt > now_utc:
                                booked.append(r)
                        except Exception:
                            booked.append(r)
                    else:
                        booked.append(r)
            return booked

        async def _fetch_doc():
            res = get_doctor_by_name(clinic_id, doctor_name)
            if hasattr(res, "__await__"):
                return await res
            return res

        # Execute non-interdependent queries in parallel via asyncio.to_thread and asyncio.gather
        holiday_data, leave_data, doc, booked_data = await asyncio.gather(
            asyncio.to_thread(_sync_fetch_holiday),
            asyncio.to_thread(_sync_fetch_leave),
            _fetch_doc(),
            asyncio.to_thread(_sync_fetch_booked),
        )

        if holiday_data:
            return [], "hospital_closed"

        blocked_sessions = []
        if leave_data:
            leave_type = leave_data[0]["leave_type"]
            if leave_type == "full":
                return [], "doctor_on_leave"
            elif leave_type == "half_morning":
                blocked_sessions = ["morning"]
            elif leave_type == "half_evening":
                blocked_sessions = ["evening"]

        if not doc:
            return [], "doctor_not_found"

        day_name = check_date.strftime("%a")
        available_days = doc.get("available_days", "Mon,Tue,Wed,Thu,Fri").split(",")
        if day_name not in available_days:
            return [], "doctor_off_day"

        raw_morn = doc.get("morning_slots")
        if raw_morn is not None:
            morning_slots = raw_morn
        elif doc.get("morning_start") is None and doc.get("morning_end") is None and "morning_slots" in doc:
            morning_slots = []
        else:
            morning_slots = ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30"]

        raw_eve = doc.get("evening_slots")
        if raw_eve is not None:
            evening_slots = raw_eve
        elif doc.get("evening_start") is None and doc.get("evening_end") is None and "evening_slots" in doc:
            evening_slots = []
        else:
            evening_slots = ["17:00", "17:30", "18:00", "18:30"]

        include_morning = "morning" not in blocked_sessions
        include_evening = "evening" not in blocked_sessions

        if branch_session == "morning":
            include_evening = False
        elif branch_session == "evening":
            include_morning = False

        all_slots = []
        if include_morning:
            all_slots.extend(morning_slots)
        if include_evening:
            all_slots.extend(evening_slots)

        # IST timezone (UTC+5:30) for cutoff calculation
        ist = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(ist)
        today_ist = now_ist.date()

        # Postgres returns a TIME column as "HH:MM:SS" while slots are "HH:MM",
        # so comparing them raw matched nothing and every booked slot stayed on
        # offer until the patient tapped it and hit the DB uniqueness guard.
        booked_times = {
            str(row["appointment_time"])[:5]
            for row in booked_data
            if row.get("appointment_time")
        }
        available = [s for s in all_slots if s not in booked_times]

        if check_date == today_ist:
            cutoff = (now_ist + timedelta(minutes=30)).strftime("%H:%M")
            available = [s for s in available if s > cutoff]

        return available, None

    except Exception as e:
        logger.error(f"Error getting available slots: {e}")
        return [], "error"


async def find_next_available_date(
    clinic_id: str, doctor_name: str, from_date_str: str
) -> tuple:
    """Find next available date with slots for a doctor."""
    from datetime import datetime, timedelta

    try:
        from_date = datetime.strptime(from_date_str, "%Y-%m-%d").date()

        for i in range(14):  # Check up to 14 days
            check_date = from_date + timedelta(days=i)
            check_date_str = check_date.strftime("%Y-%m-%d")

            # Check holiday
            holiday = (
                await sb(supabase.table("hospital_holidays")
                .select("name")
                .eq("clinic_id", clinic_id)
                .eq("holiday_date", check_date_str))
            )
            if holiday.data:
                continue

            slots, _ = await get_available_slots(clinic_id, doctor_name, check_date_str)
            if slots:
                return check_date_str, slots, None

        return None, [], "no_availability_14_days"

    except Exception as e:
        logger.error(f"Error finding next available date: {e}")
        return None, [], "error"


async def book_appointment(clinic_id: str, data: dict) -> dict:
    """Book an appointment with race condition protection.

    Uses a two-layer defense:
      1. Pre-check SELECT for fast-path rejection (avoids unnecessary writes)
      2. DB-level partial UNIQUE index (uq_appointment_active_slot) as the
         authoritative guard — closes the TOCTOU race window between the
         SELECT and INSERT.
    """
    from app.utils.helpers import (
        generate_booking_reference,
        is_booking_ref_conflict,
        is_slot_conflict,
    )

    try:
        data["clinic_id"] = clinic_id

        # Resolve doctor_id and department if missing to guarantee index and schema compatibility
        if not data.get("doctor_id") and data.get("doctor_name"):
            doc = await get_doctor_by_name(clinic_id, data["doctor_name"])
            if doc:
                if doc.get("id"):
                    data["doctor_id"] = doc["id"]
                if not data.get("department") and doc.get("department"):
                    data["department"] = doc["department"]

        if not data.get("department"):
            data["department"] = "General Medicine"

        # A consultation with no resolvable doctor_id must NOT be written.
        # Under migration 060 such rows collapsed onto a COALESCE sentinel, so
        # two different doctors falsely blocked each other while the real
        # physician went unguarded. Migration 064 keys uniqueness on doctor_id
        # directly, which means an unresolved doctor is an unguarded slot.
        # Refusing here is what keeps the index a total guarantee (KA-P0-01).
        if data.get("booking_type", "consultation") == "consultation" and not data.get(
            "doctor_id"
        ):
            logger.error(
                f"Refusing consultation booking with unresolved doctor: "
                f"clinic={clinic_id} doctor_name={data.get('doctor_name')!r} — "
                f"the slot uniqueness index cannot guard a NULL doctor_id"
            )
            return {"success": False, "reason": "doctor_unavailable"}

        # Fast-path: check for existing booking at same slot (non-authoritative).
        #
        # Deliberately NOT filtered by branch_id. The authoritative index
        # (migration 064) is keyed on clinic + doctor + date + time, and a
        # pre-check narrower than the index is worse than no pre-check: it
        # reports "free" for a slot the index will reject, and it hid the
        # KA-P0-01 double-booking because a branch-carrying booking could not
        # see a conflicting branch-less one.
        conflict_query = (
            supabase.table("appointments")
            .select("id")
            .eq("clinic_id", clinic_id)
            .eq("appointment_date", data["appointment_date"])
            .eq("appointment_time", data["appointment_time"])
            .eq("doctor_id", data["doctor_id"])
        )

        conflict = (
            await sb(conflict_query
            .in_("status", ["confirmed", "pending_payment", "pending_review"]))
        )

        if conflict.data:
            return {"success": False, "reason": "slot_taken"}

        # Bounded retry: a booking_ref collision must NOT be reported to the
        # patient as "slot_taken" (KRIYA-001). Only the partial slot unique
        # indexes mean the slot is genuinely gone.
        result = None
        for attempt in range(3):
            data["booking_ref"] = generate_booking_reference()
            try:
                # unscoped: insert_scoped_by_payload
                result = await sb(supabase.table("appointments").insert(data))
                break
            except Exception as e:
                if is_booking_ref_conflict(e) and attempt < 2:
                    logger.warning(
                        f"booking_ref collision (attempt {attempt + 1}/3), regenerating"
                    )
                    continue
                if is_slot_conflict(e):
                    return {"success": False, "reason": "slot_taken"}
                raise

        if result is None or not result.data:
            logger.error(
                "booking_ref generation exhausted 3 attempts — entropy source suspect"
            )
            return {"success": False, "reason": "internal_error"}

        # Update patient visit count
        if data.get("patient_phone"):
            patient = await get_patient_by_phone(clinic_id, data["patient_phone"])
            if patient:
                new_count = (patient.get("visit_count") or 0) + 1
                await update_patient(
                    clinic_id, data["patient_phone"], {"visit_count": new_count}
                )

        return {"success": True, "appointment": result.data[0]}

    except Exception as e:
        logger.error(f"Error booking appointment: {e}")
        if is_slot_conflict(e):
            return {"success": False, "reason": "slot_taken"}
        return {"success": False, "reason": "error"}


async def get_appointment_by_ref(clinic_id: str, booking_ref: str) -> Optional[dict]:
    """Get appointment by booking reference."""
    try:
        result = (
            await sb(scoped_query("appointments", clinic_id)
            .eq("booking_ref", booking_ref))
        )
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"Error getting appointment: {e}")
        return None


async def cancel_appointment(clinic_id: str, appointment_id: str) -> bool:
    """Cancel an appointment.

    KA-21: Only allows cancellation from non-terminal states.
    Terminal states (completed, refunded, expired, no_show) cannot be
    cancelled — they require explicit admin action.
    """
    # Allowed source states for cancellation
    CANCELLABLE_STATES = ("confirmed", "pending_payment", "pending_review")

    try:
        # First, check if the appointment has a payment_id (needs refund coupling)
        check = (
            await sb(supabase.table("appointments")
            .select("id, status, payment_id")
            .eq("clinic_id", clinic_id)
            .eq("id", appointment_id))
        )
        if not check.data:
            logger.warning(f"Cancel: appointment {appointment_id} not found in clinic {clinic_id}")
            return False

        current = check.data[0]
        current_status = current.get("status")
        payment_id = current.get("payment_id")

        if current_status not in CANCELLABLE_STATES:
            logger.warning(
                f"Cancel rejected: appointment {appointment_id} is in terminal "
                f"state '{current_status}' — cannot cancel"
            )
            return False

        if payment_id:
            # KA-21: Log that a refund may be needed — payment was captured
            logger.warning(
                f"Cancelling paid appointment {appointment_id} "
                f"(payment_id={payment_id}). Refund coordination required."
            )

        # CAS update: only cancel if still in the expected state
        result = (
            await sb(supabase.table("appointments")
            .update({"status": "cancelled"})
            .eq("clinic_id", clinic_id)
            .eq("id", appointment_id)
            .in_("status", list(CANCELLABLE_STATES)))
        )
        return bool(result.data)
    except Exception as e:
        logger.error(f"Cancel appointment error: {e}")
        return False


async def get_patient_appointments(
    clinic_id: str, phone: str, status: Optional[str] = None,
    from_date: Optional[str] = None,
) -> list:
    """Get appointments for a patient.

    Args:
        clinic_id: Tenant clinic ID.
        phone: Patient phone number.
        status: Optional status filter (e.g. 'confirmed', 'pending_payment').
        from_date: Optional YYYY-MM-DD lower bound (inclusive) on appointment_date.
                   When set, only appointments on or after this date are returned.
    """
    try:
        query = (
            scoped_query("appointments", clinic_id)
            .eq("patient_phone", phone)
        )
        if status:
            query = query.eq("status", status)
        if from_date:
            query = query.gte("appointment_date", from_date)
        result = await sb(query.order("appointment_date", desc=False))
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting patient appointments: {e}")
        return []


async def log_analytics_event(
    clinic_id: str, phone: str, event_type: str, **kwargs
) -> bool:
    """Log an analytics event."""
    try:
        data = {
            "clinic_id": clinic_id,
            "phone": phone,
            "event_type": event_type,
            **{k: v for k, v in kwargs.items() if v is not None},
        }
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("analytics_events").insert(data))
        return True
    except Exception as e:
        logger.error(f"Error logging analytics: {e}")
        return False


async def delete_patient_data(clinic_id: str, phone: str) -> bool:
    """Delete / anonymize patient data (DPDP + NMC compliance).

    Tiered strategy:
      Tier 1 (Clinical records — appointments, lab_reports, prescriptions):
        Anonymized (PII replaced with [REDACTED]), NOT deleted.
        Retained per NMC 7-year mandate for medical audit purposes.

      Tier 2 (Session / chat data — conversations, analytics):
        Fully deleted. These are transient operational records.

    This satisfies both the DPDP Act right to erasure (personal data is gone)
    and the NMC clinical record retention requirement (clinical record exists
    but contains no personally identifiable information).
    """
    try:
        # ── Tier 1: Anonymize clinical records (preserve structure, erase PII) ──
        from app.services.data_retention import data_retention_service

        await data_retention_service.anonymize_clinical_records(clinic_id, phone)

        # ── Tier 2: Delete conversation / session data ──
        await sb(supabase.table("conversations").delete().eq("clinic_id", clinic_id).eq(
            "phone", phone
        ))

        # Delete analytics events (operational, not clinical)
        try:
            await sb(supabase.table("analytics_events").delete().eq("clinic_id", clinic_id).eq(
                "phone", phone
            ))
        except Exception:
            pass  # Analytics table may not have phone column in all versions

        logger.info(
            f"Data deletion: conversations purged, clinical records anonymized "
            f"for phone={phone[:6]}*** in clinic={clinic_id}"
        )
        return True
    except Exception as e:
        logger.error(f"Error in tiered data deletion: {e}")
        return False


# Alias for backward compatibility
delete_patient = delete_patient_data


async def check_in_appointment(clinic_id: str, appointment_id: str) -> Optional[dict]:
    """Assign the next sequential token number for this appointment's doctor+date.

    Race-safe: relies on the UNIQUE partial index idx_unique_queue_token
    (migration 021) to reject collisions, and retries with the next number
    on conflict instead of allowing duplicate tokens under concurrent check-ins.
    """
    appt_result = (
        await sb(scoped_query("appointments", clinic_id, "id, clinic_id, doctor_name, appointment_date, token_number, queue_status")
        .eq("id", appointment_id))
    )
    if not appt_result.data:
        return None

    # Idempotency: if already checked in with a token, return existing record
    existing_token = appt_result.data[0].get("token_number")
    if existing_token is not None:
        logger.info(
            f"check_in_appointment: appointment {appointment_id} already checked in "
            f"with token {existing_token}"
        )
        return appt_result.data[0]

    doctor_name = appt_result.data[0]["doctor_name"]
    appointment_date = appt_result.data[0]["appointment_date"]

    max_retries = 5
    for attempt in range(max_retries):
        max_result = (
            await sb(scoped_query("appointments", clinic_id, "token_number")
            .eq("doctor_name", doctor_name)
            .eq("appointment_date", appointment_date)
            .order("token_number", desc=True)
            .limit(1))
        )
        current_max = (
            max_result.data[0]["token_number"]
            if max_result.data and max_result.data[0]["token_number"]
            else 0
        )
        next_token = current_max + 1 + attempt

        try:
            result = (
                await sb(supabase.table("appointments")
                .update({"token_number": next_token, "queue_status": "waiting"})
                .eq("clinic_id", clinic_id)
                .eq("id", appointment_id))
            )
            if result.data:
                return result.data[0]
            raise RuntimeError("Database update returned empty data")
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                logger.info(
                    f"check_in_appointment: token {next_token} collision for "
                    f"{doctor_name}/{appointment_date}, retrying (attempt {attempt + 1})"
                )
                continue
            raise

    logger.error(f"check_in_appointment: exhausted token retries for {appointment_id}")
    return None


async def call_next_patient(clinic_id: str, doctor_name: str, date_str: str) -> Optional[dict]:
    """Mark the current in_consultation patient done, and the next waiting
    patient in_consultation. Race-safe: the claiming UPDATE is conditioned
    on queue_status still being 'waiting', so a concurrent caller that
    already claimed the same row causes a retry instead of a double-serve."""
    try:
        await sb(supabase.table("appointments").update({"queue_status": "done"}).eq(
            "clinic_id", clinic_id
        ).eq("doctor_name", doctor_name).eq("appointment_date", date_str).eq(
            "queue_status", "in_consultation"
        ))

        max_retries = 5
        for _ in range(max_retries):
            next_result = (
                await sb(scoped_query("appointments", clinic_id)
                .eq("doctor_name", doctor_name)
                .eq("appointment_date", date_str)
                .eq("queue_status", "waiting")
                .order("token_number")
                .limit(1))
            )
            if not next_result.data:
                return None

            candidate = next_result.data[0]
            claimed = (
                await sb(supabase.table("appointments")
                .update({"queue_status": "in_consultation"})
                .eq("clinic_id", clinic_id)
                .eq("id", candidate["id"])
                .eq("queue_status", "waiting"))
            )
            if claimed.data:
                return claimed.data[0]

        return None
    except Exception as e:
        logger.error(f"Error calling next patient: {e}")
        return None


async def get_patient_queue_status(clinic_id: str, phone: str, date_str: str) -> Optional[dict]:
    """Look up a patient's queue position for today's appointment."""
    try:
        result = (
            await sb(scoped_query("appointments", clinic_id)
            .eq("patient_phone", phone)
            .eq("appointment_date", date_str)
            .eq("status", "confirmed"))
        )
        if not result.data:
            return None

        appt = result.data[0]
        if not appt.get("token_number"):
            return {"checked_in": False, "doctor_name": appt.get("doctor_name")}

        serving_result = (
            await sb(scoped_query("appointments", clinic_id, "token_number")
            .eq("doctor_name", appt["doctor_name"])
            .eq("appointment_date", date_str)
            .in_("queue_status", ["waiting", "in_consultation"])
            .order("token_number")
            .limit(1))
        )
        currently_serving = (
            serving_result.data[0]["token_number"] if serving_result.data else appt["token_number"]
        )
        patients_ahead = max(0, appt["token_number"] - currently_serving)

        return {
            "checked_in": True,
            "token_number": appt["token_number"],
            "currently_serving": currently_serving,
            "patients_ahead": patients_ahead,
            "doctor_name": appt["doctor_name"],
        }
    except Exception as e:
        logger.error(f"Error getting patient queue status: {e}")
        return None


async def get_family_members(clinic_id: str, primary_phone: str) -> list:
    """Return all family members registered under a primary phone number."""
    try:
        result = (
            await sb(scoped_query("family_members", clinic_id)
            .eq("primary_phone", primary_phone)
            .order("created_at"))
        )
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting family members: {e}")
        return []


async def add_family_member(
    clinic_id: str,
    primary_phone: str,
    full_name: str,
    relationship: Optional[str] = None,
) -> Optional[dict]:
    """Save a family member / dependent profile."""
    try:
        data = {
            "clinic_id": clinic_id,
            "primary_phone": primary_phone,
            "full_name": full_name,
            "relationship": relationship,
        }
        # unscoped: insert_scoped_by_payload
        result = await sb(supabase.table("family_members").insert(data))
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"Error adding family member: {e}")
        return None
