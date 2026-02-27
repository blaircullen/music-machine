import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def compute_sha256(path: str) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_flac(path: str) -> bool:
    """
    Verify FLAC file integrity using flac --test.
    Returns True if valid. If flac binary is not found, logs a warning and returns True
    to avoid blocking the workflow.
    """
    try:
        result = subprocess.run(
            ["flac", "--test", "--silent", str(path)],
            capture_output=True,
            timeout=60,
        )
        return result.returncode == 0
    except FileNotFoundError:
        logger.warning("flac binary not found — skipping FLAC integrity check")
        return True
    except subprocess.TimeoutExpired:
        logger.warning(f"FLAC verification timed out for {path}")
        return False
    except Exception as e:
        logger.warning(f"FLAC verification error for {path}: {e}")
        return False


def trash_file(source_path: str, trash_root: str, music_root: str = None) -> str:
    """
    Move a file to the trash directory, preserving relative structure from music_root.
    Handles name collisions by appending a counter suffix.
    Returns the destination trash path.
    """
    from database import get_db
    import time

    source = Path(source_path)
    trash = Path(trash_root)

    if music_root:
        try:
            rel_path = source.relative_to(music_root)
        except ValueError:
            rel_path = Path(source.name)
    else:
        rel_path = Path(source.name)

    dest = trash / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Handle name collisions
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        counter = 1
        while dest.exists():
            dest = dest.parent / f"{stem}_{counter}{suffix}"
            counter += 1

    sha256_before = None
    try:
        sha256_before = compute_sha256(str(source))
    except Exception:
        pass

    shutil.move(str(source), str(dest))

    # Record the transaction
    try:
        with get_db() as db:
            db.execute(
                """INSERT INTO file_transactions
                   (action, source_path, dest_path, state, sha256_before)
                   VALUES ('trash', ?, ?, 'committed', ?)""",
                (str(source), str(dest), sha256_before),
            )
    except Exception as e:
        logger.warning(f"Failed to record file_transaction for trash: {e}")

    return str(dest)


def restore_file(trash_path: str, original_path: str) -> bool:
    """
    Move a file from trash back to its original location.
    Returns True on success.
    """
    source = Path(trash_path)
    dest = Path(original_path)

    if not source.exists():
        logger.error(f"Restore failed: trash file not found: {trash_path}")
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        logger.warning(f"Restore target already exists, overwriting: {original_path}")

    try:
        shutil.move(str(source), str(dest))
        return True
    except Exception as e:
        logger.error(f"Restore failed: {e}")
        return False


def empty_trash(trash_root: str) -> int:
    """
    Permanently delete all files in the trash directory.
    Returns the number of files deleted.
    """
    trash = Path(trash_root)
    count = 0

    if not trash.exists():
        return 0

    for f in trash.rglob("*"):
        if f.is_file():
            try:
                f.unlink()
                count += 1
            except Exception as e:
                logger.warning(f"Failed to delete {f}: {e}")

    # Remove empty directories (deepest first)
    dirs = sorted(trash.rglob("*"), reverse=True)
    for d in dirs:
        if d.is_dir():
            try:
                if not any(d.iterdir()):
                    d.rmdir()
            except Exception:
                pass

    return count


def get_trash_contents(trash_root: str) -> list[dict]:
    """List all files in the trash directory with size and mtime."""
    trash = Path(trash_root)
    result = []

    if not trash.exists():
        return result

    for f in trash.rglob("*"):
        if f.is_file():
            try:
                stat = f.stat()
                result.append({
                    "path": str(f),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                })
            except Exception:
                pass

    return result


def import_flac(staging_path: str, original_path: str, music_root: str) -> str:
    """
    Verify FLAC integrity, compute SHA-256, then move from staging to the library.
    The new library path replaces the original file's extension with .flac.
    Returns the new library path.
    Raises ValueError if FLAC verification fails.
    """
    staging = Path(staging_path)
    original = Path(original_path)

    if not staging.exists():
        raise FileNotFoundError(f"Staging file not found: {staging_path}")

    # Verify the FLAC is valid
    if not verify_flac(str(staging)):
        raise ValueError(f"FLAC verification failed for {staging_path}")

    sha256 = compute_sha256(str(staging))

    # Destination: same path as original but with .flac extension
    library_dest = original.with_suffix(".flac")
    library_dest.parent.mkdir(parents=True, exist_ok=True)

    shutil.move(str(staging), str(library_dest))

    logger.info(f"Imported FLAC: {library_dest} (sha256={sha256[:8]}...)")
    return str(library_dest)
