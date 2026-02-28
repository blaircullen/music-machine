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
