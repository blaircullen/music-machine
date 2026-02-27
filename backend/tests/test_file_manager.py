import sys
import types

# Stub acoustid so scanner can be imported
acoustid_stub = types.ModuleType("acoustid")
acoustid_stub.fingerprint_file = lambda path: (0, None)
sys.modules.setdefault("acoustid", acoustid_stub)

# Stub database module (used by file_manager.trash_file)
import unittest.mock as mock

import pytest
from pathlib import Path
import tempfile
import shutil


@pytest.fixture
def temp_dirs():
    music_dir = Path(tempfile.mkdtemp())
    trash_dir = Path(tempfile.mkdtemp())
    test_file = music_dir / "artist" / "album" / "song.mp3"
    test_file.parent.mkdir(parents=True)
    test_file.write_bytes(b"fake audio data " * 100)
    yield music_dir, trash_dir, test_file
    shutil.rmtree(music_dir, ignore_errors=True)
    shutil.rmtree(trash_dir, ignore_errors=True)


# Patch get_db in file_manager to avoid real DB calls in tests
@pytest.fixture(autouse=True)
def patch_db(monkeypatch):
    from unittest.mock import MagicMock, patch
    import contextlib

    @contextlib.contextmanager
    def fake_get_db():
        db = MagicMock()
        db.execute.return_value = db
        yield db

    monkeypatch.setattr("file_manager.get_db", fake_get_db)


def test_compute_sha256(temp_dirs):
    from file_manager import compute_sha256
    music_dir, trash_dir, test_file = temp_dirs
    digest = compute_sha256(str(test_file))
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_compute_sha256_deterministic(temp_dirs):
    from file_manager import compute_sha256
    music_dir, trash_dir, test_file = temp_dirs
    assert compute_sha256(str(test_file)) == compute_sha256(str(test_file))


def test_trash_file_moves_file(temp_dirs):
    from file_manager import trash_file
    music_dir, trash_dir, test_file = temp_dirs
    dest = trash_file(str(test_file), str(trash_dir))
    assert not test_file.exists()
    assert Path(dest).exists()


def test_trash_file_preserves_relative_path(temp_dirs):
    from file_manager import trash_file
    music_dir, trash_dir, test_file = temp_dirs
    dest = trash_file(str(test_file), str(trash_dir), music_root=str(music_dir))
    assert "artist/album/song.mp3" in dest


def test_trash_file_handles_collision(temp_dirs):
    from file_manager import trash_file
    music_dir, trash_dir, test_file = temp_dirs

    # Create a second file with the same name
    test_file2 = music_dir / "artist2" / "album" / "song.mp3"
    test_file2.parent.mkdir(parents=True)
    test_file2.write_bytes(b"different data " * 100)

    dest1 = trash_file(str(test_file), str(trash_dir))
    dest2 = trash_file(str(test_file2), str(trash_dir))

    assert dest1 != dest2
    assert Path(dest1).exists()
    assert Path(dest2).exists()


def test_restore_file(temp_dirs):
    from file_manager import trash_file, restore_file
    music_dir, trash_dir, test_file = temp_dirs
    original_path = str(test_file)
    dest = trash_file(str(test_file), str(trash_dir), music_root=str(music_dir))
    result = restore_file(dest, original_path)
    assert result is True
    assert Path(original_path).exists()
    assert not Path(dest).exists()


def test_restore_file_missing_source(temp_dirs):
    from file_manager import restore_file
    music_dir, trash_dir, test_file = temp_dirs
    result = restore_file("/nonexistent/path/file.mp3", str(test_file))
    assert result is False


def test_empty_trash(temp_dirs):
    from file_manager import trash_file, empty_trash
    music_dir, trash_dir, test_file = temp_dirs
    trash_file(str(test_file), str(trash_dir), music_root=str(music_dir))
    count = empty_trash(str(trash_dir))
    assert count == 1
    # Trash dir should be empty (or only contain empty directories)
    remaining_files = list(trash_dir.rglob("*"))
    remaining_files = [f for f in remaining_files if f.is_file()]
    assert len(remaining_files) == 0


def test_empty_trash_nonexistent():
    from file_manager import empty_trash
    count = empty_trash("/tmp/plex-dedup-test-nonexistent-trash-12345")
    assert count == 0


def test_get_trash_contents(temp_dirs):
    from file_manager import trash_file, get_trash_contents
    music_dir, trash_dir, test_file = temp_dirs
    trash_file(str(test_file), str(trash_dir), music_root=str(music_dir))
    contents = get_trash_contents(str(trash_dir))
    assert len(contents) == 1
    assert contents[0]["size"] > 0


def test_verify_flac_missing_binary(monkeypatch):
    """verify_flac should return True if flac binary is not installed."""
    import subprocess
    from file_manager import verify_flac

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("flac not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert verify_flac("/any/path.flac") is True
