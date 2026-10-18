"""Bounded cleanup of a fixture-owned directory; persistent failures stay visible."""
import errno
import time
from pathlib import Path


def cleanup_temporary(temporary, owned_path):
    expected = Path(owned_path)
    if not expected.is_absolute() or Path(temporary.name).resolve() != expected:
        raise ValueError("temporary cleanup target changed")
    for attempt in range(6):
        try:
            temporary.cleanup()
            return
        except OSError as error:
            if (error.errno != errno.ENOTEMPTY and getattr(error, "winerror", None) not in (5, 32, 145)) or attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))
