"""A malformed id reaching Postgres (SQLSTATE 22P02) is the caller's bad input: 422, not a 500.
Other database errors that escape a route are still 500s, with no detail leaked."""
import asyncio

from postgrest.exceptions import APIError
from starlette.requests import Request

from app.main import _postgrest_error_handler, app


def _err(code):
    return APIError({"message": 'invalid input syntax for type uuid: "x"', "code": code, "details": None, "hint": None})


def _req():
    return Request({"type": "http", "method": "GET", "path": "/admin/x", "headers": [], "query_string": b""})


def test_invalid_uuid_is_422():
    res = asyncio.run(_postgrest_error_handler(_req(), _err("22P02")))
    assert res.status_code == 422 and b"Invalid identifier" in res.body


def test_other_database_errors_stay_500_without_detail():
    res = asyncio.run(_postgrest_error_handler(_req(), _err("42703")))
    assert res.status_code == 500 and b"uuid" not in res.body


def test_handler_is_registered_on_the_app():
    assert app.exception_handlers.get(APIError) is _postgrest_error_handler
