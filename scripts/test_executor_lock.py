import tempfile
import unittest
from pathlib import Path

from executor_lock import account_lock


class AccountLockTest(unittest.TestCase):
    def test_legacy_writer_excludes_second_writer_and_releases(self):
        with tempfile.TemporaryDirectory() as parent:
            with account_lock("wallet", Path(parent)):
                with self.assertRaises(RuntimeError):
                    with account_lock("WALLET", Path(parent)):
                        self.fail("duplicate writer")
            with account_lock("wallet", Path(parent)):
                pass
