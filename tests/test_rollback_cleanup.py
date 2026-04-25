import unittest
from pathlib import Path
from unittest.mock import MagicMock
import tempfile
import json
import os

from android_battery_optimizer.adb import AdbClient, CommandResult
from android_battery_optimizer.recorder import StateRecorder
from android_battery_optimizer.state import StateStore

class TestRollbackCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp_dir.name)
        self.mock_runner = MagicMock()
        self.client = AdbClient(runner=self.mock_runner, output=lambda _: None)
        self.client.serial = "test-device"
        self.store = StateStore(self.state_dir, self.client)
        self.recorder = StateRecorder(self.client, self.store)
        
        # Setup mock runner to return success for everything by default
        self.mock_runner.run.return_value = CommandResult(returncode=0, stdout="", stderr="")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _get_state_file(self):
        return self.state_dir / "devices" / "test-device" / "state.json"

    def test_revert_ledger_clears_state_file_when_all_entries_removed(self):
        # Setup: state has one entry
        self.store.data["settings"]["global/test"] = {
            "namespace": "global", "key": "test", "value": "1"
        }
        self.store.save()
        self.assertTrue(self._get_state_file().exists())

        # Action: revert a ledger with that one entry
        ledger_entry = {
            "type": "setting", "namespace": "global", "key": "test", "prior_value": "0"
        }
        self.recorder._ledger = [ledger_entry]
        self.recorder._revert_ledger([0])

        # Assert: state file is gone
        self.assertFalse(self._get_state_file().exists())
        self.assertFalse(self.store.has_entries())

    def test_revert_ledger_saves_state_when_unresolved_entries_remain(self):
        # Setup: state has two entries
        self.store.data["settings"]["global/s1"] = {"namespace": "global", "key": "s1", "value": "1"}
        self.store.data["settings"]["global/s2"] = {"namespace": "global", "key": "s2", "value": "2"}
        self.store.save()

        # Action: revert only s1, but make it FAIL
        self.mock_runner.run.return_value = CommandResult(returncode=1, stdout="", stderr="error")
        
        ledger_entry = {"type": "setting", "namespace": "global", "key": "s1", "prior_value": "0"}
        self.recorder._ledger = [ledger_entry]
        self.recorder._revert_ledger([0])

        # Assert: state file still exists and contains s1 (because it failed) and s2 (because it wasn't in the revert list)
        self.assertTrue(self._get_state_file().exists())
        self.assertTrue(self.store.has_entries())
        self.assertIn("global/s1", self.store.data["settings"])
        self.assertIn("global/s2", self.store.data["settings"])

    def test_restore_partial_failure_saves_only_unresolved_entries(self):
        # Setup: state has two entries
        self.store.data["settings"]["global/s1"] = {"namespace": "global", "key": "s1", "value": "1"}
        self.store.data["settings"]["global/s2"] = {"namespace": "global", "key": "s2", "value": "2"}
        self.store.save()

        # Mock: s1 fails to restore, s2 succeeds
        def side_effect(args, **kwargs):
            if "s1" in args:
                return CommandResult(returncode=1, stdout="", stderr="error")
            return CommandResult(returncode=0, stdout="2\n", stderr="") # s2 verification returns 2
        self.mock_runner.run.side_effect = side_effect

        # Action: restore
        self.recorder.restore()

        # Assert: state file still exists, contains s1, but s2 is gone
        self.assertTrue(self._get_state_file().exists())
        self.assertIn("global/s1", self.store.data["settings"])
        self.assertNotIn("global/s2", self.store.data["settings"])

    def test_restore_full_success_clears_state_file(self):
        # Setup: state has one entry
        self.store.data["settings"]["global/test"] = {"namespace": "global", "key": "test", "value": "1"}
        self.store.save()

        # Mock: verification succeeds
        self.mock_runner.run.return_value = CommandResult(returncode=0, stdout="1\n", stderr="")

        # Action: restore
        self.recorder.restore()

        # Assert: state file is gone
        self.assertFalse(self._get_state_file().exists())
        self.assertFalse(self.store.has_entries())

    def test_save_or_clear_respects_dry_run(self):
        # Setup: state has NO entries, but file exists
        self.store.data["settings"] = {}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "devices").mkdir(parents=True, exist_ok=True)
        (self.state_dir / "devices" / "test-device").mkdir(parents=True, exist_ok=True)
        self._get_state_file().touch()
        self.assertTrue(self._get_state_file().exists())

        # Action: call save_or_clear in dry_run
        self.client.dry_run = True
        self.store.save_or_clear()

        # Assert: file still exists
        self.assertTrue(self._get_state_file().exists())
        
        # Action: call save_or_clear in NOT dry_run
        self.client.dry_run = False
        self.store.save_or_clear()
        
        # Assert: file is gone
        self.assertFalse(self._get_state_file().exists())

if __name__ == "__main__":
    unittest.main()
