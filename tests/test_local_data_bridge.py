"""Tests for the local data bridge."""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def load_bridge_module() -> ModuleType:
    """Load the bridge module from the scripts directory."""

    module_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "local_data_bridge.py"
    )
    spec = importlib.util.spec_from_file_location("local_data_bridge", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load local_data_bridge.py for testing.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bridge() -> ModuleType:
    """Provide the bridge module to the test functions."""

    return load_bridge_module()


# --------------------------------------------------------------------------- #
# Pure helpers (SQL builder + validators)                                     #
# --------------------------------------------------------------------------- #


def test_parse_brand_ids_accepts_list_string_and_dedupes(bridge: ModuleType) -> None:
    """Brand-id input accepts both lists and comma strings, and deduplicates."""

    assert bridge.parse_brand_ids([470, 257, 470]) == (470, 257)
    assert bridge.parse_brand_ids("470, 257, 466") == (470, 257, 466)


@pytest.mark.parametrize("bad", [None, "", "470,abc,257", "470,-3", "0", [], "  "])
def test_parse_brand_ids_rejects_invalid(bridge: ModuleType, bad: Any) -> None:
    """parse_brand_ids raises BridgeError(400) for non-positive-int inputs."""

    with pytest.raises(bridge.BridgeError) as exc_info:
        bridge.parse_brand_ids(bad)
    assert exc_info.value.status_code == 400


@pytest.mark.parametrize("value, expected", [(None, 1), ("", 1), ("7", 7), (3, 3)])
def test_parse_days_defaults_and_coerces(bridge: ModuleType, value: Any, expected: int) -> None:
    """parse_days defaults missing values and coerces numeric strings."""

    assert bridge.parse_days(value) == expected


@pytest.mark.parametrize("bad", ["abc", -1, 0, 500])
def test_parse_days_rejects_out_of_range(bridge: ModuleType, bad: Any) -> None:
    """days is bounded to [1, 365]."""

    with pytest.raises(bridge.BridgeError) as exc_info:
        bridge.parse_days(bad)
    assert exc_info.value.status_code == 400


def test_build_mysql_adoption_query_substitutes_placeholders(bridge: ModuleType) -> None:
    """The SQL builder expands __BRAND_IDS__ to a %s list with matching params."""

    template = (
        "SELECT 1 FROM t WHERE created >= NOW() - INTERVAL %s DAY "
        "AND brand_id IN (__BRAND_IDS__);"
    )
    sql, params = bridge.build_mysql_adoption_query(template, (470, 257, 466), 1)

    assert "IN (%s, %s, %s)" in sql
    assert "__BRAND_IDS__" not in sql
    # First param is the day count, remaining are the brand ids in order.
    assert params == (1, 470, 257, 466)


def test_build_mysql_adoption_query_requires_placeholder(bridge: ModuleType) -> None:
    """Missing __BRAND_IDS__ surfaces a 500 BridgeError instead of bad SQL."""

    with pytest.raises(bridge.BridgeError) as exc_info:
        bridge.build_mysql_adoption_query("SELECT 1", (470,), 1)
    assert exc_info.value.status_code == 500


def test_strip_sql_line_comments_drops_double_dash_lines(bridge: ModuleType) -> None:
    """SQL line comments are stripped so stray %s in them doesn't confuse PyMySQL."""

    sql = "-- comment with %s placeholder\nSELECT %s FROM t;\n-- another %s comment\n"
    stripped = bridge.strip_sql_line_comments(sql)
    assert "%s" not in stripped.split("FROM t;")[0].splitlines()[0]
    # Surviving line keeps its real placeholder intact.
    assert "SELECT %s FROM t;" in stripped


def test_load_sql_template_strips_comments_so_pymysql_binding_works(
    bridge: ModuleType,
) -> None:
    """The loaded SQL only contains the expected number of placeholders for PyMySQL."""

    template = bridge.load_sql_template(
        Path(__file__).resolve().parents[1]
    )
    sql, params = bridge.build_mysql_adoption_query(
        template, (470, 257, 466, 416, 38, 221, 411, 301), 1
    )
    # PyMySQL internally does `query % escaped_args`. If the placeholder count
    # in the SQL doesn't match len(params), it raises TypeError. We simulate
    # that here with a tuple of correctly-escaped strings.
    escaped = tuple("'x'" for _ in params)
    rendered = sql % escaped  # Will raise TypeError if counts mismatch.
    assert rendered.count("'x'") == len(params)


def test_normalize_mysql_row_matches_dashboard_schema(bridge: ModuleType) -> None:
    """Normalizer returns the exact keys the MySQL CSV schema expects."""

    raw = {
        "brand_id": 470,
        "brand_name": "Acme  ",
        "total_sessions": 12,
        "total_utterances": 180,
        "avg_utterances_per_session": "15.00",
    }
    normalized = bridge.normalize_mysql_row(raw)
    assert set(normalized.keys()) == {
        "brand_id",
        "brand_name",
        "total_sessions",
        "total_utterances",
        "avg_utterances_per_session",
    }
    assert normalized["brand_id"] == "470"
    assert normalized["brand_name"] == "Acme"
    assert normalized["total_sessions"] == 12
    assert normalized["total_utterances"] == 180
    assert normalized["avg_utterances_per_session"] == pytest.approx(15.0)


# --------------------------------------------------------------------------- #
# CORS / origin matching                                                      #
# --------------------------------------------------------------------------- #


def test_split_allowed_origins_uses_defaults_when_empty(bridge: ModuleType) -> None:
    """Empty env value falls back to the safe default origin list."""

    assert bridge.split_allowed_origins("") == bridge.DEFAULT_ALLOWED_ORIGINS
    assert bridge.split_allowed_origins(None) == bridge.DEFAULT_ALLOWED_ORIGINS


def test_origin_allowed_matches_glob(bridge: ModuleType) -> None:
    """Glob patterns in the allowlist match concrete origins."""

    allowed = ("http://localhost:*", "https://*.netlify.app", "file://")
    assert bridge.origin_allowed("http://localhost:3000", allowed) == "http://localhost:3000"
    assert bridge.origin_allowed("https://my-app.netlify.app", allowed) == "https://my-app.netlify.app"
    assert bridge.origin_allowed("file://", allowed) == "file://"
    assert bridge.origin_allowed("https://evil.example.com", allowed) is None
    assert bridge.origin_allowed(None, allowed) is None


# --------------------------------------------------------------------------- #
# HTTP handler integration tests (full server, mocked MySQL runner)           #
# --------------------------------------------------------------------------- #


def _pick_free_port() -> int:
    """Bind to port 0 once to discover an ephemeral free port."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _start_bridge_server(
    bridge: ModuleType,
    *,
    require_auth: bool,
    bridge_token: str,
    runner: Any | None = None,
    lambda_runner: Any | None = None,
    pinot_runner: Any | None = None,
    pinot_base_url: str = "",
    pinot_auth_token: str = "",
) -> tuple[Any, threading.Thread, str]:
    """Spin up a bridge HTTP server on an ephemeral port for one test."""

    from http.server import ThreadingHTTPServer

    config = bridge.BridgeConfig(
        repo_root=Path(__file__).resolve().parents[1],
        port=_pick_free_port(),
        require_auth=require_auth,
        bridge_token=bridge_token,
        allowed_origins=bridge.DEFAULT_ALLOWED_ORIGINS,
        rds_host="localhost",
        rds_port=3306,
        rds_user="test",
        rds_region="us-west-2",
        rds_database="test",
        ca_bundle_path=Path("/tmp/test-ca.pem"),
        pinot_base_url=pinot_base_url,
        pinot_auth_token=pinot_auth_token,
    )
    kwargs: dict[str, Any] = {}
    if runner is not None:
        kwargs["mysql_runner"] = runner
    if lambda_runner is not None:
        kwargs["lambda_runner"] = lambda_runner
    if pinot_runner is not None:
        kwargs["pinot_runner"] = pinot_runner
    handler_cls = bridge.make_handler(config, **kwargs)
    server = ThreadingHTTPServer(("127.0.0.1", config.port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{config.port}"
    # Tiny wait so the socket is fully accepting connections.
    time.sleep(0.05)
    return server, thread, base_url


def _http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    """Send a tiny urllib request and return (status, headers, json body)."""

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            return resp.status, dict(resp.headers.items()), payload
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        payload = {}
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            pass
        return exc.code, dict(exc.headers.items()), payload


def test_healthz_does_not_require_token(bridge: ModuleType) -> None:
    """GET /healthz returns 200 with no X-Bridge-Token header."""

    server, _, base = _start_bridge_server(
        bridge, require_auth=True, bridge_token="secret", runner=lambda **_: [],
    )
    try:
        status, _headers, payload = _http_request("GET", base + "/healthz")
        assert status == 200
        assert payload["ok"] is True
        assert payload["version"] == bridge.BRIDGE_VERSION
        assert "mysql.adoption" in payload["datasets"]
    finally:
        server.shutdown(); server.server_close()


def test_post_query_requires_token(bridge: ModuleType) -> None:
    """A POST without X-Bridge-Token returns 401 + JSON error."""

    server, _, base = _start_bridge_server(
        bridge, require_auth=True, bridge_token="secret", runner=lambda **_: [],
    )
    try:
        status, _h, payload = _http_request(
            "POST",
            base + "/query/mysql/adoption",
            headers={"Content-Type": "application/json"},
            body={"brand_ids": [470], "days": 1},
        )
        assert status == 401
        assert "X-Bridge-Token" in payload["error"]
    finally:
        server.shutdown(); server.server_close()


def test_post_query_accepts_valid_token_and_returns_rows(bridge: ModuleType) -> None:
    """Valid token + body produces the runner's rows verbatim in the response."""

    sample_rows = [
        {"brand_id": "470", "brand_name": "Acme",
         "total_sessions": 12, "total_utterances": 180,
         "avg_utterances_per_session": 15.0},
    ]

    def fake_runner(*, config: Any, brand_ids: tuple[int, ...], days: int) -> list[dict[str, Any]]:
        """Stand-in for fetch_mysql_adoption that asserts the wiring."""

        assert brand_ids == (470, 257)
        assert days == 7
        return sample_rows

    server, _, base = _start_bridge_server(
        bridge, require_auth=True, bridge_token="secret", runner=fake_runner,
    )
    try:
        status, _h, payload = _http_request(
            "POST",
            base + "/query/mysql/adoption",
            headers={"Content-Type": "application/json", "X-Bridge-Token": "secret"},
            body={"brand_ids": [470, 257], "days": 7},
        )
        assert status == 200
        assert payload["row_count"] == 1
        assert payload["rows"] == sample_rows
        assert payload["days"] == 7
        assert payload["brand_ids"] == [470, 257]
    finally:
        server.shutdown(); server.server_close()


def test_post_query_validates_inputs(bridge: ModuleType) -> None:
    """Invalid brand_ids return 400 before the runner is invoked."""

    runner_called = {"value": False}
    def fake_runner(**_: Any) -> list[dict[str, Any]]:
        runner_called["value"] = True
        return []

    server, _, base = _start_bridge_server(
        bridge, require_auth=False, bridge_token="", runner=fake_runner,
    )
    try:
        status, _h, payload = _http_request(
            "POST",
            base + "/query/mysql/adoption",
            headers={"Content-Type": "application/json"},
            body={"brand_ids": ["not-an-int"], "days": 1},
        )
        assert status == 400
        assert "brand_ids" in payload["error"]
        assert runner_called["value"] is False
    finally:
        server.shutdown(); server.server_close()


def test_assert_select_only_accepts_select(bridge: ModuleType) -> None:
    """A vanilla SELECT statement passes the safety guard."""

    bridge.assert_select_only("SELECT 1 FROM t WHERE a > 0;")
    bridge.assert_select_only("-- comment\nSELECT *\nFROM t;")


@pytest.mark.parametrize(
    "bad_sql",
    [
        "UPDATE t SET a=1",
        "DROP TABLE t",
        "DELETE FROM t WHERE 1=1",
        "INSERT INTO t VALUES (1)",
        "SELECT 1; DROP TABLE t;",   # two statements
        "ALTER TABLE t ADD COLUMN x INT",
        "TRUNCATE TABLE t",
        "EXEC sp_drop",
        "WITH x AS (SELECT 1) DELETE FROM t",  # DELETE buried after a CTE
        "",
        "   -- only a comment",
    ],
)
def test_assert_select_only_rejects_non_selects(bridge: ModuleType, bad_sql: str) -> None:
    """Anything that isn't a single SELECT raises BridgeError(500)."""

    with pytest.raises(bridge.BridgeError) as exc_info:
        bridge.assert_select_only(bad_sql)
    assert exc_info.value.status_code == 500


def test_build_pinot_latency_query_substitutes_window_and_brand_ids(bridge: ModuleType) -> None:
    """The Pinot query builder fills both placeholders and passes the guard."""

    template = (
        "SELECT brand_id FROM prod_service_metrics\n"
        "WHERE first_chunk_timestamp >= now() - __WINDOW_MS__\n"
        "AND brand_id IN (__BRAND_IDS__)"
    )
    rendered = bridge.build_pinot_latency_query(template, (470, 257, 466), 86_400_000)
    assert "86400000" in rendered
    assert "IN (470, 257, 466)" in rendered
    assert "__WINDOW_MS__" not in rendered
    assert "__BRAND_IDS__" not in rendered


def test_build_pinot_latency_query_requires_both_placeholders(bridge: ModuleType) -> None:
    """A template missing either placeholder raises BridgeError before sending."""

    with pytest.raises(bridge.BridgeError):
        bridge.build_pinot_latency_query("SELECT 1", (470,), 86_400_000)


def test_load_pinot_template_returns_select_only_after_substitution(bridge: ModuleType) -> None:
    """The shipped Pinot SQL file, when rendered, satisfies the safety guard."""

    template = bridge.load_pinot_template(Path(__file__).resolve().parents[1])
    rendered = bridge.build_pinot_latency_query(template, (470, 257), 86_400_000)
    # If this returns without raising, the SELECT-only guard accepted it.
    bridge.assert_select_only(rendered)


def test_normalize_pinot_row_matches_dashboard_schema(bridge: ModuleType) -> None:
    """Normalized Pinot rows present the 11-column first/follow-up schema."""

    columns = [
        "brand_id",
        "first_total_requests", "first_avg_ttfb_ms",
        "first_p50_ttfb_ms", "first_p95_ttfb_ms", "first_p99_ttfb_ms",
        "followup_total_requests", "followup_avg_ttfb_ms",
        "followup_p50_ttfb_ms", "followup_p95_ttfb_ms", "followup_p99_ttfb_ms",
    ]
    values = [
        411,
        18, 2900.5, 2400.0, 6800.85, 7400.0,
        39, 2400.6, 2100.0, 6100.12, 6900.0,
    ]
    row = bridge._normalize_pinot_row(columns, values)
    assert set(row.keys()) == set(columns)
    assert row["brand_id"] == "411"
    assert row["first_total_requests"] == "18"
    assert row["followup_total_requests"] == "39"
    # Floats are rendered with two decimals.
    assert row["first_avg_ttfb_ms"] == "2900.50"
    assert row["first_p95_ttfb_ms"] == "6800.85"
    assert row["followup_p95_ttfb_ms"] == "6100.12"


def test_fetch_pinot_latency_happy_path(bridge: ModuleType) -> None:
    """End-to-end runner test with a mocked HTTP and template loader."""

    def fake_http(*, url: str, body: bytes, bearer_token: str, timeout: int) -> Any:
        """Stand-in for _post_pinot_sql returning a first/followup result set."""

        assert "/sql" in url
        assert bearer_token == "fake-pinot-jwt"
        request_payload = json.loads(body.decode("utf-8"))
        # The SQL must be SELECT and contain the substituted window + brand_ids.
        assert request_payload["sql"].lstrip().upper().startswith("SELECT")
        assert "86400000" in request_payload["sql"]
        assert "IN (411)" in request_payload["sql"]
        canned = {
            "resultTable": {
                "dataSchema": {
                    "columnNames": [
                        "brand_id",
                        "first_total_requests", "first_avg_ttfb_ms",
                        "first_p50_ttfb_ms", "first_p95_ttfb_ms", "first_p99_ttfb_ms",
                        "followup_total_requests", "followup_avg_ttfb_ms",
                        "followup_p50_ttfb_ms", "followup_p95_ttfb_ms",
                        "followup_p99_ttfb_ms",
                    ],
                    "columnDataTypes": [
                        "INT", "LONG", "DOUBLE", "DOUBLE", "DOUBLE", "DOUBLE",
                        "LONG", "DOUBLE", "DOUBLE", "DOUBLE", "DOUBLE",
                    ],
                },
                "rows": [[
                    411,
                    18, 2900.5, 2400.0, 6800.85, 7400.0,
                    39, 2400.6, 2100.0, 6100.12, 6900.0,
                ]],
            },
            "numRowsResultSet": 1,
            "exceptions": [],
            "numDocsScanned": 57,
            "timeUsedMs": 3,
        }
        return bridge._PinotHttpResponse(200, json.dumps(canned))

    template = (
        "SELECT brand_id FROM t WHERE first_chunk_timestamp >= now() - __WINDOW_MS__ "
        "AND brand_id IN (__BRAND_IDS__)"
    )
    result = bridge.fetch_pinot_latency(
        config=bridge.BridgeConfig(
            repo_root=Path(__file__).resolve().parents[1],
            port=0, require_auth=False, bridge_token="",
            allowed_origins=bridge.DEFAULT_ALLOWED_ORIGINS,
            rds_host="", rds_port=3306, rds_user="", rds_region="us-west-2",
            rds_database="", ca_bundle_path=Path("/tmp/ca.pem"),
            pinot_base_url="https://pinot.example/cp.s7e.startree.cloud",
            pinot_auth_token="",
        ),
        brand_ids=(411,), days=1,
        pinot_auth_token="fake-pinot-jwt",
        http_runner=fake_http,
        template_loader=lambda _: template,
    )
    assert result["window_ms"] == 86_400_000
    assert result["num_docs_scanned"] == 57
    assert result["time_used_ms"] == 3
    assert len(result["rows"]) == 1
    assert result["rows"][0]["brand_id"] == "411"
    assert result["rows"][0]["first_p95_ttfb_ms"] == "6800.85"
    assert result["rows"][0]["followup_p95_ttfb_ms"] == "6100.12"


def test_fetch_pinot_latency_raises_on_pinot_exceptions(bridge: ModuleType) -> None:
    """A 200 response with non-empty exceptions[] bubbles up as 502."""

    def fake_http(**_: Any) -> Any:
        """Return a Pinot response advertising a query exception."""

        payload = {
            "resultTable": None,
            "exceptions": [{"errorCode": 200, "message": "Bad expression"}],
        }
        return bridge._PinotHttpResponse(200, json.dumps(payload))

    cfg = bridge.BridgeConfig(
        repo_root=Path(__file__).resolve().parents[1],
        port=0, require_auth=False, bridge_token="",
        allowed_origins=bridge.DEFAULT_ALLOWED_ORIGINS,
        rds_host="", rds_port=3306, rds_user="", rds_region="us-west-2",
        rds_database="", ca_bundle_path=Path("/tmp/ca.pem"),
        pinot_base_url="https://pinot.example",
        pinot_auth_token="",
    )
    with pytest.raises(bridge.BridgeError) as exc_info:
        bridge.fetch_pinot_latency(
            config=cfg, brand_ids=(411,), days=1,
            pinot_auth_token="t", http_runner=fake_http,
            template_loader=lambda _: (
                "SELECT 1 FROM t WHERE x >= now() - __WINDOW_MS__ AND y IN (__BRAND_IDS__)"
            ),
        )
    assert exc_info.value.status_code == 502
    assert "Bad expression" in exc_info.value.message


def test_fetch_pinot_latency_requires_token_and_base_url(bridge: ModuleType) -> None:
    """Missing token / base URL raises the friendliest possible error."""

    def fake_http(**_: Any) -> Any:
        """Should never be called when validation aborts upstream."""

        raise AssertionError("HTTP runner should not be invoked")

    cfg_no_url = bridge.BridgeConfig(
        repo_root=Path(__file__).resolve().parents[1],
        port=0, require_auth=False, bridge_token="",
        allowed_origins=bridge.DEFAULT_ALLOWED_ORIGINS,
        rds_host="", rds_port=3306, rds_user="", rds_region="us-west-2",
        rds_database="", ca_bundle_path=Path("/tmp/ca.pem"),
        pinot_base_url="", pinot_auth_token="",
    )
    with pytest.raises(bridge.BridgeError) as exc_no_url:
        bridge.fetch_pinot_latency(
            config=cfg_no_url, brand_ids=(411,), days=1,
            pinot_auth_token="t", http_runner=fake_http,
            template_loader=lambda _: "SELECT 1 FROM t",
        )
    assert exc_no_url.value.status_code == 500
    assert "PINOT_BASE_URL" in exc_no_url.value.message

    cfg_no_token = bridge.BridgeConfig(
        repo_root=Path(__file__).resolve().parents[1],
        port=0, require_auth=False, bridge_token="",
        allowed_origins=bridge.DEFAULT_ALLOWED_ORIGINS,
        rds_host="", rds_port=3306, rds_user="", rds_region="us-west-2",
        rds_database="", ca_bundle_path=Path("/tmp/ca.pem"),
        pinot_base_url="https://pinot.example", pinot_auth_token="",
    )
    with pytest.raises(bridge.BridgeError) as exc_no_token:
        bridge.fetch_pinot_latency(
            config=cfg_no_token, brand_ids=(411,), days=1,
            pinot_auth_token="", http_runner=fake_http,
            template_loader=lambda _: "SELECT 1 FROM t",
        )
    assert exc_no_token.value.status_code == 401


def test_post_pinot_latency_happy_path_through_http(bridge: ModuleType) -> None:
    """End-to-end: server-side POST validates inputs and returns runner rows."""

    runner_calls: list[dict[str, Any]] = []

    def fake_pinot_runner(*, config: Any, brand_ids: tuple[int, ...], days: int,
                          pinot_auth_token: str) -> dict[str, Any]:
        """Capture call args and emit canned rows."""

        runner_calls.append({
            "brand_ids": brand_ids, "days": days, "token": pinot_auth_token,
        })
        return {
            "rows": [{
                "brand_id": "411", "total_requests": "57",
                "avg_ttfb_ms": "2740.09", "p50_ttfb_ms": "2381.00",
                "p95_ttfb_ms": "6483.85", "p99_ttfb_ms": "6701.36",
            }],
            "window_ms": 86_400_000,
            "num_docs_scanned": 57,
            "time_used_ms": 3,
        }

    server, _, base = _start_bridge_server(
        bridge, require_auth=True, bridge_token="pinot-secret",
        pinot_runner=fake_pinot_runner,
        pinot_base_url="https://pinot.example",
    )
    try:
        status, _h, payload = _http_request(
            "POST",
            base + "/query/pinot/latency",
            headers={"Content-Type": "application/json", "X-Bridge-Token": "pinot-secret"},
            body={
                "days": 1,
                "brand_ids": [411],
                "pinot_auth_token": "jwt-from-paste",
            },
        )
        assert status == 200
        assert payload["row_count"] == 1
        assert payload["window_ms"] == 86_400_000
        assert payload["num_docs_scanned"] == 57
        assert payload["time_used_ms"] == 3
        assert runner_calls and runner_calls[0] == {
            "brand_ids": (411,), "days": 1, "token": "jwt-from-paste",
        }
    finally:
        server.shutdown(); server.server_close()


def test_post_pinot_latency_falls_back_to_env_token(bridge: ModuleType) -> None:
    """If the body omits pinot_auth_token, the bridge uses the env var default."""

    captured = {}

    def fake_pinot_runner(*, config: Any, brand_ids: tuple[int, ...], days: int,
                          pinot_auth_token: str) -> dict[str, Any]:
        """Record which token the bridge handed us."""

        captured["token"] = pinot_auth_token
        return {"rows": [], "window_ms": days * 86_400_000}

    server, _, base = _start_bridge_server(
        bridge, require_auth=False, bridge_token="",
        pinot_runner=fake_pinot_runner,
        pinot_auth_token="env-token",
    )
    try:
        status, _h, _payload = _http_request(
            "POST",
            base + "/query/pinot/latency",
            headers={"Content-Type": "application/json"},
            body={"days": 1, "brand_ids": [411]},
        )
        assert status == 200
        assert captured["token"] == "env-token"
    finally:
        server.shutdown(); server.server_close()


def test_cors_preflight_echoes_allowed_origin(bridge: ModuleType) -> None:
    """OPTIONS responses echo back the request Origin when allowlisted."""

    server, _, base = _start_bridge_server(
        bridge, require_auth=False, bridge_token="", runner=lambda **_: [],
    )
    try:
        status, headers, _payload = _http_request(
            "OPTIONS",
            base + "/query/mysql/adoption",
            headers={
                "Origin": "http://localhost:8080",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert status == 204
        assert headers.get("Access-Control-Allow-Origin") == "http://localhost:8080"
        assert "X-Bridge-Token" in headers.get("Access-Control-Allow-Headers", "")
    finally:
        server.shutdown(); server.server_close()


def test_cors_preflight_omits_origin_for_disallowed(bridge: ModuleType) -> None:
    """An origin outside the allowlist gets no Allow-Origin header echoed back."""

    server, _, base = _start_bridge_server(
        bridge, require_auth=False, bridge_token="", runner=lambda **_: [],
    )
    try:
        status, headers, _payload = _http_request(
            "OPTIONS",
            base + "/query/mysql/adoption",
            headers={"Origin": "https://evil.example.com"},
        )
        assert status == 204
        assert "Access-Control-Allow-Origin" not in headers
    finally:
        server.shutdown(); server.server_close()


def test_parse_lambda_names_falls_back_to_defaults_when_blank(bridge: ModuleType) -> None:
    """Empty input pulls the canonical default lambda set for the env."""

    fakes = {
        "prod": ("zen-prod-sqs-message-consumer", "zen-prod-ws-to-sqs-producer"),
    }

    def default_for_env(env: str) -> tuple[str, ...]:
        """Look up canonical defaults by env name for the test."""

        return fakes[env]

    assert bridge.parse_lambda_names(
        None, default_for_env=default_for_env, env_name="prod"
    ) == fakes["prod"]
    assert bridge.parse_lambda_names(
        "  ", default_for_env=default_for_env, env_name="prod"
    ) == fakes["prod"]


def test_parse_lambda_names_accepts_csv_or_list(bridge: ModuleType) -> None:
    """CSV string and list inputs both produce trimmed tuples."""

    def default_for_env(_: str) -> tuple[str, ...]:
        """Unused default-supplier when an explicit list is provided."""

        return ()

    assert bridge.parse_lambda_names(
        "alpha, beta, , gamma",
        default_for_env=default_for_env, env_name="prod",
    ) == ("alpha", "beta", "gamma")
    assert bridge.parse_lambda_names(
        ["alpha", " beta "],
        default_for_env=default_for_env, env_name="prod",
    ) == ("alpha", "beta")


def test_parse_ignore_messages_handles_blank(bridge: ModuleType) -> None:
    """parse_ignore_messages returns () for empty input and tuple for csv."""

    assert bridge.parse_ignore_messages(None) == ()
    assert bridge.parse_ignore_messages("") == ()
    assert bridge.parse_ignore_messages("a, b") == ("a", "b")
    assert bridge.parse_ignore_messages(["one", " two"]) == ("one", "two")


def test_post_lambda_cloudwatch_happy_path(bridge: ModuleType) -> None:
    """End-to-end: the lambda endpoint validates inputs and returns runner rows."""

    runner_calls: list[dict[str, Any]] = []
    sample_row = {
        "lambda_name": "zen-prod-sqs-message-consumer",
        "invocations": "571",
        "lambda_errors": "0",
        "throttles": "0",
        "avg_duration_ms": "760.15",
        "p95_duration_ms": "3354.11",
        "p99_duration_ms": "5222.44",
        "max_duration_ms": "7015.35",
        "cold_starts": "19",
        "avg_init_duration_ms": "1605.81",
        "warning_count": "1",
        "error_count": "0",
        "top_errors": "",
        "top_warnings": "stream_runner.llm_streaming.failed:1",
    }

    def fake_lambda_runner(*, config: Any, env_name: str, region: str, days: int,
                           lambda_names: tuple[str, ...],
                           ignored_messages: tuple[str, ...],
                           use_default_ignores: bool) -> dict[str, Any]:
        """Capture the call args and return canned rows + window info."""

        runner_calls.append({
            "env_name": env_name, "region": region, "days": days,
            "lambda_names": lambda_names,
            "ignored_messages": ignored_messages,
            "use_default_ignores": use_default_ignores,
        })
        return {
            "rows": [sample_row],
            "start_time": "2026-05-13T08:30:00Z",
            "end_time": "2026-05-14T08:30:00Z",
            "ignored_messages": list(ignored_messages) + ["should_flush.emergency_accumulation"],
        }

    server, _, base = _start_bridge_server(
        bridge, require_auth=True, bridge_token="lambda-secret",
        lambda_runner=fake_lambda_runner,
    )
    try:
        status, _h, payload = _http_request(
            "POST",
            base + "/query/lambda/cloudwatch",
            headers={"Content-Type": "application/json", "X-Bridge-Token": "lambda-secret"},
            body={
                "env": "prod",
                "region": "us-west-1",
                "days": 1,
                "lambda_names": ["zen-prod-sqs-message-consumer"],
                "ignore_messages": ["noisy_warning"],
                "use_default_ignores": True,
            },
        )
        assert status == 200
        assert payload["row_count"] == 1
        assert payload["rows"][0] == sample_row
        assert payload["env"] == "prod"
        assert payload["region"] == "us-west-1"
        assert payload["start_time"] == "2026-05-13T08:30:00Z"
        assert payload["ignored_messages"] == [
            "noisy_warning", "should_flush.emergency_accumulation",
        ]
        assert runner_calls and runner_calls[0]["lambda_names"] == ("zen-prod-sqs-message-consumer",)
        assert runner_calls[0]["ignored_messages"] == ("noisy_warning",)
    finally:
        server.shutdown(); server.server_close()


def test_post_lambda_cloudwatch_requires_token(bridge: ModuleType) -> None:
    """Lambda endpoint also rejects calls missing the bridge token."""

    server, _, base = _start_bridge_server(
        bridge, require_auth=True, bridge_token="lambda-secret",
    )
    try:
        status, _h, payload = _http_request(
            "POST",
            base + "/query/lambda/cloudwatch",
            headers={"Content-Type": "application/json"},
            body={"days": 1},
        )
        assert status == 401
        assert "X-Bridge-Token" in payload["error"]
    finally:
        server.shutdown(); server.server_close()


def test_post_lambda_cloudwatch_validates_days(bridge: ModuleType) -> None:
    """days outside [1, 365] returns 400 before the runner is invoked."""

    def fake_lambda_runner(**_: Any) -> dict[str, Any]:
        """Should never run because validation aborts upstream."""

        raise AssertionError("runner should not be called for bad days")

    server, _, base = _start_bridge_server(
        bridge, require_auth=False, bridge_token="",
        lambda_runner=fake_lambda_runner,
    )
    try:
        status, _h, payload = _http_request(
            "POST",
            base + "/query/lambda/cloudwatch",
            headers={"Content-Type": "application/json"},
            body={"days": 0},
        )
        assert status == 400
        assert "days" in payload["error"]
    finally:
        server.shutdown(); server.server_close()


def test_fetch_lambda_cloudwatch_surfaces_collector_failure(bridge: ModuleType,
                                                            tmp_path: Path) -> None:
    """A non-zero collector return code converts into a 502 BridgeError."""

    class FakeCompleted:
        """Minimal subprocess.CompletedProcess stand-in."""

        def __init__(self, returncode: int, stderr: str) -> None:
            self.returncode = returncode
            self.stderr = stderr
            self.stdout = ""

    def fake_runner(_command: list[str], *, cwd: str, timeout: int) -> FakeCompleted:
        """Pretend the shell collector failed with rc=2."""

        return FakeCompleted(returncode=2, stderr="boom: missing aws creds")

    # Build a minimal but real BridgeConfig pointing at this repo so the
    # collector script path resolution does not 500 before the runner runs.
    repo_root = Path(__file__).resolve().parents[1]
    config = bridge.BridgeConfig(
        repo_root=repo_root,
        port=0, require_auth=False, bridge_token="",
        allowed_origins=bridge.DEFAULT_ALLOWED_ORIGINS,
        rds_host="", rds_port=3306, rds_user="",
        rds_region="us-west-2", rds_database="",
        ca_bundle_path=tmp_path / "ca.pem",
    )

    # Stub the review module so we don't have to actually load run_cloudwatch_review.
    class FakeReview:
        """Minimal stand-in exposing only the functions fetch_lambda_cloudwatch uses."""

        sanitize_for_path = staticmethod(lambda value: value.replace(":", "-"))

    with pytest.raises(bridge.BridgeError) as exc_info:
        bridge.fetch_lambda_cloudwatch(
            config=config,
            env_name="prod",
            region="us-west-2",
            days=1,
            lambda_names=("zen-prod-sqs-message-consumer",),
            ignored_messages=(),
            use_default_ignores=True,
            subprocess_runner=fake_runner,
            review_module=FakeReview,
        )
    assert exc_info.value.status_code == 502
    assert "rc=2" in exc_info.value.message
    assert "missing aws creds" in exc_info.value.message


def test_unknown_post_returns_404(bridge: ModuleType) -> None:
    """An unknown POST path returns a JSON 404."""

    server, _, base = _start_bridge_server(
        bridge, require_auth=False, bridge_token="", runner=lambda **_: [],
    )
    try:
        status, _h, payload = _http_request(
            "POST",
            base + "/query/unknown",
            headers={"Content-Type": "application/json"},
            body={},
        )
        assert status == 404
        assert "POST" in payload["error"]
    finally:
        server.shutdown(); server.server_close()
