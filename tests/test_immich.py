"""ImmichClient tests against a stub HTTP server.

These do not prove the paths in config.example.yaml match your Immich release —
only `--check-api` against a real instance can do that. What they prove is that
pagination, chunking, parsing and the spec check behave correctly given a
server that responds in the documented shape.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tripcluster.config import ApiConfig  # noqa: E402
from tripcluster.immich import ImmichClient, ImmichError, parse_asset  # noqa: E402

SPEC = {
    "paths": {
        "/api/search/metadata": {"post": {}},
        "/api/albums": {"post": {}, "get": {}},
        "/api/albums/{id}": {"patch": {}, "get": {}, "delete": {}},
        "/api/albums/{id}/assets": {"put": {}, "delete": {}},
    }
}


class Stub(BaseHTTPRequestHandler):
    calls: list[tuple[str, str, dict]] = []
    pages = 2

    def log_message(self, *a):  # keep pytest output clean
        pass

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _send(self, obj, code=200):
        payload = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.headers.get("x-api-key") != "test-key":
            return self._send({"error": "unauthorized"}, 401)
        if self.path == "/api/specs-json":
            return self._send(SPEC)
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        body = self._body()
        Stub.calls.append(("POST", self.path, body))
        if self.path == "/api/search/metadata":
            page = body.get("page", 1)
            if page > Stub.pages:
                return self._send({"assets": {"items": [], "nextPage": None}})
            items = [
                {
                    "id": f"p{page}-{i}",
                    "exifInfo": {
                        "dateTimeOriginal": "2023-09-02T08:00:00.000-07:00",
                        "latitude": 36.17,
                        "longitude": -115.14,
                        "timeZone": "America/Los_Angeles",
                    },
                }
                for i in range(3)
            ]
            nxt = page + 1 if page < Stub.pages else None
            return self._send({"assets": {"items": items, "nextPage": nxt}})
        if self.path == "/api/albums":
            return self._send({"id": "album-abc", "albumName": body.get("albumName")})
        self._send({"error": "not found"}, 404)

    def do_PUT(self):
        body = self._body()
        Stub.calls.append(("PUT", self.path, body))
        self._send([{"id": i, "success": True} for i in body.get("ids", [])])

    def do_PATCH(self):
        Stub.calls.append(("PATCH", self.path, self._body()))
        self._send({"id": "album-abc"})


@pytest.fixture
def server():
    Stub.calls = []
    Stub.pages = 2
    httpd = HTTPServer(("127.0.0.1", 0), Stub)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.fixture
def client(server):
    return ImmichClient(ApiConfig(base_url=server, key="test-key", page_size=3))


def test_missing_key_fails_before_any_request():
    with pytest.raises(ImmichError, match="IMMICH_API_KEY"):
        ImmichClient(ApiConfig(base_url="http://example.invalid", key=""))


def test_spec_check_passes_on_matching_paths(client):
    assert client.verify_spec() == []


def test_spec_check_reports_a_moved_endpoint(server):
    cfg = ApiConfig(base_url=server, key="test-key",
                    create_album="/api/album")   # the pre-1.106 spelling
    problems = ImmichClient(cfg).verify_spec()
    assert len(problems) == 1
    assert "/api/album not found" in problems[0]


def test_spec_check_reports_a_missing_method(server):
    cfg = ApiConfig(base_url=server, key="test-key",
                    add_album_assets="/api/albums/{id}")   # exists, but no PUT
    problems = ImmichClient(cfg).verify_spec()
    assert any("no PUT" in p for p in problems)


def test_spec_check_survives_an_unreachable_spec(server):
    cfg = ApiConfig(base_url=server, key="test-key", openapi_spec="/api/nope")
    problems = ImmichClient(cfg).verify_spec()
    assert len(problems) == 1
    assert "could not fetch OpenAPI spec" in problems[0]


def test_pagination_stops_at_the_last_page(client):
    assets = list(client.iter_assets())
    assert len(assets) == 6
    assert {a["id"] for a in assets} == {f"p{p}-{i}" for p in (1, 2) for i in range(3)}


def test_album_create_chunks_oversized_payloads(client):
    ids = [f"a{i}" for i in range(2400)]
    assert client.create_album("Trip", "desc", ids) == "album-abc"

    posts = [c for c in Stub.calls if c[0] == "POST" and c[1] == "/api/albums"]
    assert len(posts[0][2]["assetIds"]) == 1000

    puts = [c for c in Stub.calls if c[0] == "PUT"]
    assert sum(len(c[2]["ids"]) for c in puts) == 1400
    assert all(len(c[2]["ids"]) <= 500 for c in puts)


def test_update_album_omits_the_name_when_hand_edited(client):
    client.update_album("album-abc", name=None, description="new description")
    patch = [c for c in Stub.calls if c[0] == "PATCH"][0]
    assert "albumName" not in patch[2]
    assert patch[2]["description"] == "new description"


def test_http_errors_are_wrapped(client):
    with pytest.raises(ImmichError, match="404"):
        client._request("GET", "/api/does-not-exist")


# ---- parsing --------------------------------------------------------------

def test_parse_asset_reads_exif_offset():
    asset = parse_asset({
        "id": "x",
        "exifInfo": {
            "dateTimeOriginal": "2023-09-02T08:00:00.000-07:00",
            "latitude": 36.17,
            "longitude": -115.14,
        },
    })
    assert asset.tz_offset_min == -420
    assert asset.local_dt.hour == 8


def test_parse_asset_reads_iana_timezone_names():
    asset = parse_asset({
        "id": "x",
        "exifInfo": {
            "dateTimeOriginal": "2023-09-02T15:00:00.000Z",
            "timeZone": "America/Denver",
        },
    })
    assert asset.tz_offset_min == -360


def test_parse_asset_reads_utc_offset_strings():
    asset = parse_asset({
        "id": "x",
        "exifInfo": {"dateTimeOriginal": "2023-09-02T15:00:00.000Z",
                     "timeZone": "UTC+5:30"},
    })
    assert asset.tz_offset_min == 330


def test_parse_asset_falls_back_through_timestamp_fields():
    asset = parse_asset({"id": "x", "fileCreatedAt": "2023-09-02T15:00:00.000Z"})
    assert asset is not None
    assert asset.lat is None


def test_parse_asset_rejects_unusable_records():
    assert parse_asset({"id": "x"}) is None
    assert parse_asset({"exifInfo": {"dateTimeOriginal": "2023-09-02T15:00:00Z"}}) is None
    assert parse_asset({"id": "x", "fileCreatedAt": "not a date"}) is None


def test_parse_asset_keeps_zero_coordinates():
    """0.0 is a real longitude. Truthiness checks would drop Greenwich."""
    asset = parse_asset({
        "id": "x",
        "exifInfo": {"dateTimeOriginal": "2023-09-02T15:00:00.000Z",
                     "latitude": 51.48, "longitude": 0.0},
    })
    assert asset.lon == 0.0
    assert asset.has_fix is True
