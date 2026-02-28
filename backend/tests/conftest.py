import os
import tempfile
from pathlib import Path
import pytest
from database import init_db
import database


def pytest_configure(config):
    """Set DB_PATH to a temp file before any module imports database."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="plex-dedup-test-")
    os.close(fd)
    os.environ["DB_PATH"] = path


@pytest.fixture(scope="session", autouse=True)
def init_test_db():
    db_path = os.environ.get("DB_PATH", "")
    database.DB_PATH = Path(db_path)
    init_db()
    yield
    path = Path(db_path)
    if path.exists():
        path.unlink()
