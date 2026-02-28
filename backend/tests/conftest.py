"""
Test configuration: redirect DB_PATH to a writable temp location and
initialize the schema so endpoint tests can hit real SQLite tables.
"""
import os
import tempfile
import pytest
import database


def pytest_configure(config):
    """Set DB_PATH env var before any modules are imported by test files."""
    # Only redirect if still pointing at the Docker volume path
    if os.environ.get("DB_PATH", "/data/plex-dedup.db").startswith("/data"):
        _tmp = tempfile.mktemp(suffix=".db", prefix="plex-dedup-test-")
        os.environ["DB_PATH"] = _tmp


@pytest.fixture(autouse=True, scope="session")
def init_test_db():
    """Patch database.DB_PATH to match the env var and run schema init."""
    from pathlib import Path
    db_path = Path(os.environ.get("DB_PATH", "/data/plex-dedup.db"))
    database.DB_PATH = db_path
    database.init_db()
    yield
