"""ImmichClient tests against a stub HTTP server.

These do not prove the paths in config.example.yaml match your Immich release —
only `--check-api` against a real instance can do that. What they prove is that
pagination, chunking, parsing and the spec check behave correctly given a
server that responds in the documented shape.
"""

from __future__ import annotations

import json
import re
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
    # Real Immich refuses unauthenticated writes, and the endpoint probe in
    # verify_spec depends on that: 401 means the route exists, 404 means it
    # doesn't. Set False to simulate an instance with auth switched off.
    require_auth = True
    # When True the stub also returns Live Photo video halves, which real
    # Immich marks visibility=hidden and omits from album counts.
    include_hidden = False

    def log_message(self, *a):  # keep pytest output clean
        pass

    # Real Immich resolves the route *before* authenticating: an unknown path
    # is 404 even without credentials, a known one is 401. The endpoint probe
    # depends on that ordering, so the stub has to reproduce it.
    ROUTES = {
        "POST":  (r"^/api/search/metadata$", r"^/api/albums$"),
        "PUT":   (r"^/api/albums/[^/]+/assets$",),
        "PATCH": (r"^/api/albums/[^/]+$",),
    }

    def _routed(self, method: str) -> bool:
        if any(re.match(p, self.path) for p in Stub.ROUTES.get(method, ())):
            return True
        self._send({"error": "not found"}, 404)
        return False

    def _authed(self) -> bool:
        if not Stub.require_auth:
            return True
        if self.headers.get("x-api-key") == "test-key":
            return True
        self._send({"error": "unauthorized"}, 401)
        return False

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
        if not self._routed("POST") or not self._authed():
            return
        body = self._body()
        Stub.calls.append(("POST", self.path, body))
        if self.path == "/api/search/metadata":
            page = body.get("page", 1)
            if page > Stub.pages:
                return self._send({"assets": {"items": [], "nextPage": None}})
            # Immich omits exifInfo unless withExif is requested. Reproducing
            # that here is the point: without it the caller still gets assets
            # and timestamps, just no coordinates, and nothing errors.
            with_exif = bool(body.get("withExif"))
            items = []
            for i in range(3):
                item: dict = {
                    "id": f"p{page}-{i}",
                    "fileCreatedAt": "2023-09-02T08:00:00.000-07:00",
                }
                if with_exif:
                    item["exifInfo"] = {
                        "dateTimeOriginal": "2023-09-02T08:00:00.000-07:00",
                        "latitude": 36.17,
                        "longitude": -115.14,
                        "timeZone": "America/Los_Angeles",
                    }
                items.append(item)
                if Stub.include_hidden:
                    # The video half of a Live Photo, as Immich returns it.
                    items.append({
                        "id": f"hidden{page}-{i}",
                        "type": "VIDEO",
                        "visibility": "hidden",
                        "fileCreatedAt": "2023-09-02T08:00:00.000-07:00",
                    })
            nxt = page + 1 if page < Stub.pages else None
            return self._send({"assets": {"items": items, "nextPage": nxt}})
        if self.path == "/api/albums":
            return self._send({"id": "album-abc", "albumName": body.get("albumName")})
        self._send({"error": "not found"}, 404)

    def do_PUT(self):
        if not self._routed("PUT") or not self._authed():
            return
        body = self._body()
        Stub.calls.append(("PUT", self.path, body))
        self._send([{"id": i, "success": True} for i in body.get("ids", [])])

    def do_PATCH(self):
        if not self._routed("PATCH") or not self._authed():
            return
        Stub.calls.append(("PATCH", self.path, self._body()))
        self._send({"id": "album-abc"})


@pytest.fixture
def server():
    Stub.calls = []
    Stub.pages = 2
    Stub.require_auth = True
    Stub.include_hidden = False
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


def test_no_spec_falls_back_to_probing_endpoints(server):
    """Immich 3.1.0 publishes no OpenAPI document under /api.

    A missing spec used to be reported as the single problem, which made
    --check-api useless on exactly the instances it needed to verify. It now
    probes each configured endpoint instead: correct paths answer 401, so an
    instance with no spec and correct paths is clean.
    """
    cfg = ApiConfig(base_url=server, key="test-key", openapi_spec="/api/nope")
    assert ImmichClient(cfg).verify_spec() == []


def test_probe_reports_a_moved_endpoint_without_a_spec(server):
    cfg = ApiConfig(base_url=server, key="test-key", openapi_spec="/api/nope",
                    search_assets="/api/search/moved")
    problems = ImmichClient(cfg).verify_spec()
    assert len(problems) == 1
    assert "404" in problems[0] and "api.search_assets" in problems[0]


def test_probe_does_not_authenticate(server):
    """The probe must not carry the API key.

    POST /api/albums with credentials creates an album. If the probe ever
    authenticates, every --check-api run litters the library with empty
    albums, so assert the stub saw no authenticated write.
    """
    cfg = ApiConfig(base_url=server, key="test-key", openapi_spec="/api/nope")
    ImmichClient(cfg).verify_spec()
    assert Stub.calls == []


def test_probe_flags_an_instance_that_allows_unauthenticated_writes(server):
    """An open instance must fail the check, not quietly pass it."""
    Stub.require_auth = False
    cfg = ApiConfig(base_url=server, key="test-key", openapi_spec="/api/nope")
    problems = ImmichClient(cfg).verify_spec()
    assert problems and all("WITHOUT credentials" in p for p in problems)


def test_search_requests_exif_and_gets_coordinates(client):
    """Immich 3.1.0 omits exifInfo unless withExif is set on the request.

    The failure this guards is silent: without the flag every asset still
    arrives with an id and a timestamp, parse_asset accepts it, has_fix is
    False for all of them, and segment_trips returns no trips at all. Measured
    against the live instance, where the search response carried 28 keys and
    no coordinate among them.
    """
    assets = [parse_asset(a) for a in client.iter_assets()]
    assert all(a is not None and a.has_fix for a in assets)

    posts = [c for c in Stub.calls if c[1] == "/api/search/metadata"]
    assert posts and all(c[2].get("withExif") is True for c in posts)
    # Same trap, same fix: without withPeople every asset reports nobody in it.
    assert all(c[2].get("withPeople") is True for c in posts)


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


def test_unreachable_host_is_wrapped_not_raw(client):
    """A down server must not surface as a urllib3 traceback.

    The weekly run is a cron job; a 60-line stack ending in
    NewConnectionError says nothing about the actual cause, which is almost
    always a wrong base_url or a stopped container.
    """
    cfg = ApiConfig(base_url="http://127.0.0.1:1", key="test-key")
    with pytest.raises(ImmichError, match="cannot reach Immich"):
        ImmichClient(cfg)._request("GET", "/api/anything")


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


def test_hidden_live_photo_videos_are_skipped(server):
    """23% of the real library is `type=VIDEO visibility=hidden`.

    Immich stores the video half of every Live Photo as its own asset and
    excludes it from an album's assetCount. Counting them as photos inflated
    every album total by about a quarter — 173 sent, 110 counted — made
    min_photos quietly lenient, and let one Live Photo vote twice on a stop.
    """
    Stub.include_hidden = True
    cfg = ApiConfig(base_url=server, key="test-key", page_size=3)
    ids = [a["id"] for a in ImmichClient(cfg).iter_assets()]
    assert ids, "sanity: the stub returned something"
    assert not any(i.startswith("hidden") for i in ids)


def test_assets_without_a_visibility_field_are_kept(server):
    """Older releases have no such concept; absent must not mean excluded."""
    Stub.include_hidden = False
    cfg = ApiConfig(base_url=server, key="test-key", page_size=3)
    assert len(list(ImmichClient(cfg).iter_assets())) == 6
