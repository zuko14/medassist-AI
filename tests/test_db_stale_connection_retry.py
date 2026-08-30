# tests/test_db_stale_connection_retry.py
"""httpx reuses pooled keep-alive connections; PostgREST closes idle ones on
its side, so a request can fail with RemoteProtocolError("Server disconnected")
before it ever reaches the server. Seen in production as
"Failed to get diagnostic reports queue: Server disconnected"."""

import httpx
import pytest

# NB: import sb INSIDE each test, never at module scope.
# tests/test_conversation_payment_mode.py installs a fake app.database into
# sys.modules at import time, and pytest imports every test module during
# collection — so a module-level `from app.database import sb` here binds that
# fake's inline mock instead of the real function, and the retry never runs.


class _Builder:
    def __init__(self, method, fail_times=1):
        self.request = type("Cfg", (), {"http_method": method})()
        self.calls = 0
        self._fail_times = fail_times

    def execute(self):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD", "PATCH", "DELETE"])
async def test_stale_connection_is_retried_for_replayable_methods(method):
    from app.database import sb

    b = _Builder(method)
    assert await sb(b) == "ok"
    assert b.calls == 2


@pytest.mark.asyncio
async def test_insert_is_never_replayed():
    """A POST that died mid-flight may already have committed; replaying it
    would duplicate the row."""
    from app.database import sb

    b = _Builder("POST")
    with pytest.raises(httpx.RemoteProtocolError):
        await sb(b)
    assert b.calls == 1


@pytest.mark.asyncio
async def test_only_one_retry():
    """A genuinely down server must surface, not spin."""
    from app.database import sb

    b = _Builder("GET", fail_times=99)
    with pytest.raises(httpx.RemoteProtocolError):
        await sb(b)
    assert b.calls == 2


@pytest.mark.asyncio
async def test_real_postgrest_builder_exposes_the_method():
    """Guards the attribute path sb() reads; a supabase-py change that moves
    http_method would silently disable the retry."""
    from app.database import supabase

    assert supabase.table("t").select("id").request.http_method.value == "GET"
    assert supabase.table("t").insert({"x": 1}).request.http_method.value == "POST"
