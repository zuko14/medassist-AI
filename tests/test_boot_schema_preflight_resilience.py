"""The boot-time schema pre-flight must ride out a transport outage.

Schema DRIFT is fatal on purpose — serving traffic against a database that is
missing migrations corrupts data. But an unreachable database is a different
condition: crashing there puts the deployment platform into a restart loop for
the whole duration of the outage, so the pre-flight is documented to log
critically and start anyway, degrading DB-backed routes to 503.

The handler originally listed only ConnectError, TimeoutException and OSError.
Behind an egress proxy — the norm on hospital networks — a hiccup arrives as
httpx.ProxyError, and a dropped connection as ReadError or
RemoteProtocolError. Each of those fell through to the fatal branch and
crash-looped the app on precisely the outage it was meant to survive.
"""

import inspect

import httpx
import pytest

import app.main as main_module


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("timed out"),
        httpx.ProxyError("403 Forbidden"),
        httpx.ReadError("connection dropped"),
        httpx.RemoteProtocolError("server disconnected"),
        OSError("dns failure"),
    ],
)
def test_transport_failures_are_tolerated_by_the_preflight_handler(exc):
    """Every one of these must be caught by the connectivity branch."""
    source = inspect.getsource(main_module.lifespan)
    assert "except (httpx.TransportError, OSError)" in source, (
        "the pre-flight no longer catches the whole transport family"
    )
    # httpx.TransportError is the family the handler relies on; OSError covers
    # the socket-level failures httpx does not wrap.
    assert isinstance(exc, (httpx.TransportError, OSError))


def test_schema_drift_is_still_fatal():
    """The fail-closed guard must survive the widened transport handling."""
    source = inspect.getsource(main_module.lifespan)
    assert "Schema drift detected" in source
    # RuntimeError is re-raised ahead of the connectivity branch, so drift
    # cannot be mistaken for an outage and swallowed.
    assert source.index("except RuntimeError:") < source.index(
        "except (httpx.TransportError, OSError)"
    )
