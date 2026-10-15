"""Run supabase-py / postgrest query builders against a REAL Postgres.

Used by the IPD end-to-end tests: the HTTP routes execute their real queries,
SQL functions, triggers and constraints - nothing in the data path is mocked.
Only the PostgREST HTTP hop is replaced, by a translation of the request the
builder would have sent (path, method, query params, JSON body, Prefer/Accept).

Scope: the queries the /admin/ipd routes and the OPD invoice-issue path make:
select (columns, eq/neq/in/is/gt/gte/lt/lte/like/ilike and their not.* forms,
order, limit/offset, count=exact, single), insert (dict or list), update,
delete, rpc. Embedded resources (`branches(id, name)`) are omitted, not joined.
Anything else raises loudly. It is a test harness for IPD, not a PostgREST
replacement: other modules' routes are covered by their own suites, so a gap
there is noted, not emulated here.
"""

import json
import threading
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlparse

import psycopg2
import psycopg2.extras
from postgrest._sync import request_builder as rb
from postgrest.exceptions import APIError

_OPS = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "like": "LIKE", "ilike": "ILIKE"}
_RESERVED = {"select", "order", "limit", "offset", "on_conflict", "columns"}


def _ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"bridge: unsupported identifier {name!r}")
    return f'"{name}"'


class PgBridge:
    def __init__(self, uri: str):
        self.uri = uri
        self._local = threading.local()
        self._argtypes: dict[str, dict[str, str]] = {}
        self.calls: list[tuple[str, str]] = []
        self._opened: list = []          # every connection, so close() can release them all
        self._lock = threading.Lock()

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.closed:
            conn = psycopg2.connect(self.uri)
            conn.autocommit = True
            self._local.conn = conn
            with self._lock:
                self._opened.append(conn)
        return conn

    def close(self) -> None:
        """The sb() executor threads outlive a test; without this each test leaked
        one connection per thread and later real-Postgres suites hit max_connections."""
        with self._lock:
            for conn in self._opened:
                conn.close()
            self._opened.clear()

    # ── translation ───────────────────────────────────────────────────────
    def _where(self, params) -> tuple[str, list]:
        clauses, args = [], []
        for key, raw in params:
            if key in _RESERVED:
                continue
            op, _, val = raw.partition(".")
            col = _ident(key)
            if op == "not":   # not.is.null / not.eq.x
                op, _, val = val.partition(".")
                if op == "is":
                    clauses.append(f"{col} IS NOT {'NULL' if val == 'null' else val.upper()}")
                    continue
                if op not in _OPS:
                    raise ValueError(f"bridge: unsupported filter {key}={raw}")
                clauses.append(f"NOT ({col} {_OPS[op]} %s)")
                args.append(val)
                continue
            if op in _OPS:
                if op in ("like", "ilike"):
                    val = val.replace("*", "%")
                clauses.append(f"{col} {_OPS[op]} %s")
                args.append(val)
            elif op == "in":
                items = [v.strip().strip('"') for v in val.strip("()").split(",") if v.strip()]
                if not items:
                    clauses.append("false")
                else:
                    clauses.append(f"{col} IN ({', '.join(['%s'] * len(items))})")
                    args.extend(items)
            elif op == "is":
                clauses.append(f"{col} IS {'NULL' if val == 'null' else val.upper()}")
            else:
                raise ValueError(f"bridge: unsupported filter {key}={raw}")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args

    @staticmethod
    def _order(params) -> str:
        for key, raw in params:
            if key == "order":
                parts = []
                for item in raw.split(","):
                    bits = item.split(".")
                    direction = "DESC" if "desc" in bits[1:] else "ASC"
                    nulls = " NULLS FIRST" if "nullsfirst" in bits else " NULLS LAST" if "nullslast" in bits else ""
                    parts.append(f"{_ident(bits[0])} {direction}{nulls}")
                return " ORDER BY " + ", ".join(parts)
        return ""

    def _rpc_types(self, cur, fn: str) -> dict[str, str]:
        if fn not in self._argtypes:
            cur.execute("""SELECT unnest(proargnames), unnest(proargtypes::regtype[]::text[])
                             FROM pg_proc WHERE proname = %s""", (fn,))
            self._argtypes[fn] = dict(cur.fetchall())
        return self._argtypes[fn]

    def execute(self, builder):
        req = builder.request
        path = urlparse(str(req.path)).path
        method = str(getattr(req.http_method, "value", req.http_method)).upper()
        params = list(parse_qsl(str(req.params), keep_blank_values=True))
        prefer = str(req.headers.get("Prefer") or "")
        single = "vnd.pgrst.object" in str(req.headers.get("Accept") or "")
        self.calls.append((method, path))
        cur = self._conn().cursor()
        try:
            if "/rpc/" in path:
                fn = path.rsplit("/", 1)[-1]
                types = self._rpc_types(cur, fn)
                body = req.json or {}
                named = ", ".join(f"{_ident(k)} => %s::{types[k]}" for k in body)
                values = [psycopg2.extras.Json(v) if isinstance(v, (dict, list)) and types[k].startswith("json")
                          else v for k, v in body.items()]
                cur.execute(f"SELECT to_jsonb({_ident(fn)}({named}))", values)
                return SimpleNamespace(data=cur.fetchone()[0], count=None)
            table = _ident(path.rstrip("/").rsplit("/", 1)[-1])
            where, args = self._where(params)
            if method == "GET":
                cols = dict(params).get("select", "*")
                # shortcut: embedded resources ("branches(id, name)") are omitted, not joined; no IPD route
                # uses embedding. Upgrade if a test needs a route that reads embedded rows.
                plain, depth, cur_col = [], 0, ""
                for ch in cols + ",":
                    depth += ch == "("
                    depth -= ch == ")"
                    if ch == "," and depth == 0:
                        plain.append(cur_col.strip())
                        cur_col = ""
                    else:
                        cur_col += ch
                plain = [c for c in plain if c and "(" not in c]
                select = "*" if plain in ([], ["*"]) else ", ".join("*" if c == "*" else _ident(c) for c in plain)
                q = dict(params)
                tail = self._order(params)
                tail += f" LIMIT {int(q['limit'])}" if "limit" in q else ""
                tail += f" OFFSET {int(q['offset'])}" if "offset" in q else ""
                cur.execute(f"SELECT to_jsonb(t) FROM (SELECT {select} FROM {table}{where}{tail}) t", args)
                rows = [r[0] for r in cur.fetchall()]
                count = None
                if "count=exact" in prefer:
                    cur.execute(f"SELECT count(*) FROM {table}{where}", args)
                    count = cur.fetchone()[0]
            elif method == "POST":
                body = req.json if isinstance(req.json, list) else [req.json]
                keys = sorted({k for row in body for k in row})
                cols = ", ".join(_ident(k) for k in keys)
                cur.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM jsonb_populate_recordset("
                            f"NULL::{table}, %s) RETURNING to_jsonb({table}.*)", [psycopg2.extras.Json(body)])
                rows, count = [r[0] for r in cur.fetchall()], None
            elif method == "PATCH":
                keys = list(req.json)
                cols = ", ".join(_ident(k) for k in keys)
                sets = f"({cols}) = (SELECT {cols} FROM jsonb_populate_record(NULL::{table}, %s))" if len(keys) > 1 \
                    else f"{cols} = (SELECT {cols} FROM jsonb_populate_record(NULL::{table}, %s))"
                cur.execute(f"UPDATE {table} SET {sets}{where} RETURNING to_jsonb({table}.*)",
                            [psycopg2.extras.Json(req.json), *args])
                rows, count = [r[0] for r in cur.fetchall()], None
            elif method == "DELETE":
                cur.execute(f"DELETE FROM {table}{where} RETURNING to_jsonb({table}.*)", args)
                rows, count = [r[0] for r in cur.fetchall()], None
            else:
                raise ValueError(f"bridge: unsupported method {method}")
        except psycopg2.Error as e:
            diag = e.diag
            raise APIError({"message": diag.message_primary or str(e), "code": e.pgcode,
                            "details": diag.message_detail, "hint": diag.constraint_name}) from None
        finally:
            cur.close()
        if single:
            if len(rows) != 1:
                raise APIError({"message": "JSON object requested, multiple (or no) rows returned",
                                "code": "PGRST116", "details": None, "hint": None})
            return SimpleNamespace(data=rows[0], count=count)
        return SimpleNamespace(data=rows, count=count)

    def install(self, monkeypatch):
        bridge = self
        for cls in (rb.SyncQueryRequestBuilder, rb.SyncSingleRequestBuilder, rb.SyncMaybeSingleRequestBuilder):
            monkeypatch.setattr(cls, "execute", lambda self, _b=bridge: _b.execute(self))
        return self


def dumps(obj) -> str:
    return json.dumps(obj, default=str)
