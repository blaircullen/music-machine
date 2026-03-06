from fastapi.testclient import TestClient
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from main import app

client = TestClient(app)

def test_search_accepts_scope_body():
    """POST /api/upgrades/search should accept scope params without error."""
    res = client.post("/api/upgrades/search", json={
        "format_filter": "mp3",
        "unscanned_only": True,
        "batch_size": 10,
        "artist_filter": None,
    })
    # Returns ok:true or ok:false (already running) — either is valid
    assert res.status_code == 200
    data = res.json()
    assert "ok" in data


def test_coverage_endpoint():
    res = client.get("/api/upgrades/coverage")
    assert res.status_code == 200
    data = res.json()
    for key in ("total_candidates", "scanned", "unscanned", "found", "completed"):
        assert key in data
        assert isinstance(data[key], int)


def test_unscanned_endpoint():
    res = client.get("/api/upgrades/unscanned")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    if data:
        row = data[0]
        for key in ("track_id", "artist", "album", "title", "format", "bitrate"):
            assert key in row


def test_approve_hi_res_endpoint():
    res = client.post("/api/upgrades/approve-hi-res")
    assert res.status_code == 200
    data = res.json()
    assert "ok" in data
    assert "approved" in data
    assert isinstance(data["approved"], int)
