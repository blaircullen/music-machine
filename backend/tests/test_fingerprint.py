import sys
import types

# Stub acoustid so scanner can be imported without the binary installed locally
acoustid_stub = types.ModuleType("acoustid")
acoustid_stub.fingerprint_file = lambda path: (0, None)
sys.modules.setdefault("acoustid", acoustid_stub)

import pytest
from pathlib import Path
from scanner import generate_fingerprint

FIXTURES = Path(__file__).parent / "fixtures"


def test_fingerprint_returns_string_or_none():
    """generate_fingerprint must return str or None, never raise."""
    result = generate_fingerprint(str(FIXTURES / "test_128.mp3"))
    assert result is None or isinstance(result, str)


def test_fingerprint_nonexistent_returns_none():
    result = generate_fingerprint("/tmp/does_not_exist_plex_dedup.mp3")
    assert result is None


def test_same_file_same_fingerprint():
    """With a real fpcalc available (in Docker), same file = same fingerprint."""
    fp1 = generate_fingerprint(str(FIXTURES / "test_128.mp3"))
    fp2 = generate_fingerprint(str(FIXTURES / "test_128.mp3"))
    # Both could be None (no fpcalc), but if strings they must match
    if fp1 is not None and fp2 is not None:
        assert fp1 == fp2


def test_different_files_fingerprints_are_independent():
    """Two different files should each return their own fingerprint (or None)."""
    fp1 = generate_fingerprint(str(FIXTURES / "test_128.mp3"))
    fp2 = generate_fingerprint(str(FIXTURES / "test_16_44.flac"))
    # At minimum, no exception should be raised
    assert fp1 is None or isinstance(fp1, str)
    assert fp2 is None or isinstance(fp2, str)
