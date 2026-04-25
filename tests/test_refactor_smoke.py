import unittest
from android_battery_optimizer.recorder import StateRecorder
from android_battery_optimizer.snapshot import SnapshotError
from android_battery_optimizer.verification import VerificationError
from android_battery_optimizer.ledger import AnyLedgerEntry

class TestRefactorSmoke(unittest.TestCase):
    def test_public_methods_exist(self):
        self.assertTrue(hasattr(StateRecorder, "transaction"))
        self.assertTrue(hasattr(StateRecorder, "prefetch_package_states"))
        self.assertTrue(hasattr(StateRecorder, "snapshot_setting"))
        self.assertTrue(hasattr(StateRecorder, "put_setting"))
        self.assertTrue(hasattr(StateRecorder, "delete_setting"))
        self.assertTrue(hasattr(StateRecorder, "snapshot_device_config"))
        self.assertTrue(hasattr(StateRecorder, "put_device_config"))
        self.assertTrue(hasattr(StateRecorder, "delete_device_config"))
        self.assertTrue(hasattr(StateRecorder, "snapshot_package_enabled"))
        self.assertTrue(hasattr(StateRecorder, "set_package_enabled"))
        self.assertTrue(hasattr(StateRecorder, "snapshot_appop"))
        self.assertTrue(hasattr(StateRecorder, "set_appop"))
        self.assertTrue(hasattr(StateRecorder, "snapshot_standby_bucket"))
        self.assertTrue(hasattr(StateRecorder, "set_standby_bucket"))
        self.assertTrue(hasattr(StateRecorder, "verify_setting"))
        self.assertTrue(hasattr(StateRecorder, "verify_device_config"))
        self.assertTrue(hasattr(StateRecorder, "verify_appop"))
        self.assertTrue(hasattr(StateRecorder, "verify_standby_bucket"))
        self.assertTrue(hasattr(StateRecorder, "verify_package_enabled"))
        self.assertTrue(hasattr(StateRecorder, "restore"))

    def test_errors_exported(self):
        # Verify imports work
        self.assertIsNotNone(SnapshotError)
        self.assertIsNotNone(VerificationError)
