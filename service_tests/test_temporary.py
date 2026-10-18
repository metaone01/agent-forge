import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from service_tests.temporary import cleanup_temporary


class TemporaryCleanupTests(unittest.TestCase):
    def fixture(self):
        root = Path(tempfile.gettempdir()).resolve() / "forge-cleanup-fixture"
        return Mock(name=str(root)), root

    def test_transient_empty_directory_error_is_retried(self):
        temporary, root = self.fixture()
        temporary.name = str(root)
        temporary.cleanup.side_effect = [OSError(errno.ENOTEMPTY, "busy"), None]
        with patch("service_tests.temporary.time.sleep") as sleep:
            cleanup_temporary(temporary, root)
        self.assertEqual(2, temporary.cleanup.call_count)
        sleep.assert_called_once_with(0.05)

    def test_persistent_cleanup_failure_is_not_hidden(self):
        temporary, root = self.fixture()
        temporary.name = str(root)
        temporary.cleanup.side_effect = OSError(errno.ENOTEMPTY, "busy")
        with patch("service_tests.temporary.time.sleep"):
            with self.assertRaises(OSError):
                cleanup_temporary(temporary, root)
        self.assertEqual(6, temporary.cleanup.call_count)

    def test_unrelated_error_is_not_retried(self):
        temporary, root = self.fixture()
        temporary.name = str(root)
        temporary.cleanup.side_effect = OSError(errno.EIO, "disk")
        with self.assertRaises(OSError):
            cleanup_temporary(temporary, root)
        temporary.cleanup.assert_called_once_with()

    def test_changed_target_is_rejected_before_cleanup(self):
        temporary, root = self.fixture()
        temporary.name = str(root.parent / "another-target")
        with self.assertRaises(ValueError):
            cleanup_temporary(temporary, root)
        temporary.cleanup.assert_not_called()
