"""Exercise the installer's release selection with local Git repositories."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


CHECKOUT = Path(__file__).resolve().parents[1] / "deploy" / "checkout-release.sh"
BASH = shutil.which("bash")
if os.name == "nt" and shutil.which("git"):
    BASH = str(Path(shutil.which("git")).parents[1] / "bin" / "bash.exe")


@unittest.skipUnless(BASH and Path(BASH).is_file() and shutil.which("git"), "requires Bash and Git")
class ReleaseSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.remote = base / "remote"
        self.work = base / "deployment"
        self.git(base, "init", "-b", "main", str(self.remote))
        self.git(self.remote, "config", "user.name", "test")
        self.git(self.remote, "config", "user.email", "test@localhost")
        self.first = self.commit("first")
        self.git(self.remote, "checkout", "-b", "release/candidate")
        self.second = self.commit("second")
        self.git(self.remote, "checkout", "main")

    @staticmethod
    def git(path, *args):
        return subprocess.check_output(
            ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE,
        ).strip()

    def commit(self, value):
        (self.remote / "version").write_text(value)
        self.git(self.remote, "add", "version")
        self.git(self.remote, "commit", "-m", value)
        return self.git(self.remote, "rev-parse", "HEAD")

    def deploy(self, ref, success=True):
        result = subprocess.run(
            [BASH, CHECKOUT.as_posix(), self.remote.as_posix(), self.work.as_posix(), ref],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.assertEqual(result.returncode == 0, success, result.stdout)
        return result.stdout

    def test_branch_install_and_pinned_update_ignore_default_and_current_branch(self):
        self.deploy("main")
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), self.first)
        self.git(self.work, "checkout", "-b", "other")
        output = self.deploy(self.second.upper())
        self.assertIn(self.first, output)
        self.assertIn(self.second, output)
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), self.second)
        self.assertEqual((self.work / "version").read_text(), "second")

    def test_pinned_install_and_branch_update_with_local_runtime_data(self):
        self.work.mkdir()
        self.deploy(self.first.upper())
        log = self.work / "data/live_trade_final/today/events.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text("record")
        (self.work / ".venv").mkdir()
        (self.work / ".venv/pyvenv.cfg").touch()
        (self.work / "data/dry_run").mkdir()
        (self.work / "data/dry_run/events.jsonl").touch()
        self.deploy("release/candidate")
        self.assertEqual(log.read_text(), "record")
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), self.second)

    def test_unknown_target_dirty_tree_and_non_fast_forward_preserve_work(self):
        self.deploy("release/candidate")
        self.deploy("missing", success=False)
        self.deploy("main", success=False)
        self.assertEqual((self.work / "version").read_text(), "second")
        self.git(self.remote, "checkout", "release/candidate")
        self.commit("third")
        (self.work / "version").write_text("operator edit")
        self.deploy("release/candidate", success=False)
        self.assertEqual((self.work / "version").read_text(), "operator edit")
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), self.second)
        self.git(self.work, "restore", "version")
        (self.work / "note.txt").write_text("operator note")
        self.deploy("release/candidate", success=False)
        self.assertEqual((self.work / "note.txt").read_text(), "operator note")
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), self.second)

    def test_nonempty_directory_without_repo_is_untouched(self):
        self.work.mkdir()
        (self.work / "version").write_text("local")
        self.deploy("main", success=False)
        self.assertFalse((self.work / ".git").exists())
        self.assertEqual((self.work / "version").read_text(), "local")

    def test_ignored_state_colliding_with_target_is_preserved(self):
        self.deploy(self.first)
        (self.work / ".git/info/exclude").write_text("state.db\n")
        (self.work / "state.db").write_text("operator state")
        (self.remote / "state.db").write_text("repository copy")
        self.git(self.remote, "add", "state.db")
        self.commit("third")
        self.deploy("main", success=False)
        self.assertEqual((self.work / "state.db").read_text(), "operator state")
        self.assertEqual((self.work / "version").read_text(), "first")
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), self.first)
