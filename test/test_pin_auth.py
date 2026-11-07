"""Security regression tests. All panel commands below are offline fakes."""

# Keep this suite runnable with unittest inside HA containers without pytest.
# ruff: noqa: PT009, PT027, PLC0415

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "pin_auth_under_test",
    Path(__file__).resolve().parents[1] / "custom_components/qolsys_panel/pin_auth.py",
)
assert spec is not None and spec.loader is not None
auth = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = auth
spec.loader.exec_module(auth)

PIN_A = "8247109362"  # Synthetic test credentials only.
PIN_B = "3719068245"


class StoreTests(unittest.TestCase):
    hash_a: str
    hash_b: str

    @classmethod
    def setUpClass(cls):
        cls.hash_a = auth.make_hash(PIN_A)
        cls.hash_b = auth.make_hash(PIN_B)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "users.conf"

    def write(self, records):
        auth.write_private_json(self.path, records)

    def test_salt_is_unique_and_parameters_are_fixed(self):
        again = auth.make_hash(PIN_A)
        self.assertNotEqual(again, self.hash_a)
        self.assertTrue(again.startswith("$argon2id$v=19$m=65536,t=3,p=4$"))
        self.assertTrue(auth._valid_hash(again))

    def test_hash_only_owner_only_roundtrip(self):
        self.write([{"id": 1, "user_code_hash": self.hash_a}])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(PIN_A, self.path.read_text())
        self.assertEqual(auth.verify_pin(self.path, PIN_A, frozenset({1})), 1)
        self.assertEqual(auth.verify_pin(self.path, PIN_B, frozenset({1})), -1)
        self.assertEqual(auth.verify_pin(self.path, PIN_A, frozenset({2})), -1)
        self.assertEqual(auth.verify_pin(self.path, self.hash_a, frozenset({1})), -1)
        self.assertEqual(list(self.path.parent.glob(".pin-*")), [])

    def test_missing_file_rejects(self):
        self.assertEqual(auth.verify_pin(self.path, PIN_A, frozenset({1})), -1)

    def test_four_digit_pin_preserves_leading_zero(self):
        pin = "0729"  # Synthetic test PIN only.
        self.write([{"id": 1, "user_code_hash": auth.make_hash(pin)}])
        self.assertEqual(auth.verify_pin(self.path, pin, frozenset({1})), 1)
        self.assertEqual(auth.verify_pin(self.path, "0728", frozenset({1})), -1)
        self.assertEqual(auth.verify_pin(self.path, "729", frozenset({1})), -1)
        self.assertNotIn(json.dumps(pin), self.path.read_text())

    def test_insecure_or_malformed_stores_reject(self):
        invalid = [
            [{"id": 1, "user_code": PIN_A}],
            [{"id": True, "user_code_hash": self.hash_a}],
            [{"id": 0, "user_code_hash": self.hash_a}],
            [{"id": 1, "user_code_hash": self.hash_a}] * 2,
            [
                {
                    "id": 1,
                    "user_code_hash": self.hash_a.replace("m=65536", "m=999999999"),
                }
            ],
            [{"id": 1, "user_code_hash": "sha256$pretend"}],
            {"id": 1, "user_code_hash": self.hash_a},
        ]
        for records in invalid:
            with self.subTest(records_type=type(records).__name__):
                self.write(records)
                with self.assertRaises(auth.PinStoreError):
                    auth.load_records(self.path)
        self.write([{"id": 1, "user_code_hash": self.hash_a}])
        self.path.chmod(0o644)
        with self.assertRaises(auth.PinStoreError):
            auth.load_records(self.path)

    def test_symlink_is_refused(self):
        other = self.path.with_name("other")
        auth.write_private_json(other, [])
        self.path.symlink_to(other)
        with self.assertRaises(auth.PinStoreError):
            auth.load_records(self.path)
        with self.assertRaises(auth.PinStoreError):
            self.write([])

    def test_short_and_non_ascii_pins_reject(self):
        for pin in ("123", "a" * 10, "１" * 4, "9" * 13, None):
            with self.assertRaises(auth.PinStoreError):
                auth.make_hash(pin)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "users.conf"
        records = [
            {"id": 1, "user_code_hash": auth.make_hash(PIN_A)},
            {"id": 2, "user_code_hash": auth.make_hash(PIN_B)},
        ]
        auth.write_private_json(self.path, records)
        self.calls: list[tuple[str, int]] = []
        self.database_users = [
            {"userid": "1", "username": "Test A", "userPin": "NEVER EXPORT"},
            {"userid": "2", "username": "Test B"},
        ]
        self.controller = SimpleNamespace(
            settings=SimpleNamespace(
                users_file_path=self.path,
                check_user_code_on_arm=False,
                check_user_code_on_disarm=True,
            ),
            panel=SimpleNamespace(
                _users=["old plaintext reader disabled"],
                db=SimpleNamespace(get_users=lambda: self.database_users),
            ),
            commands=SimpleNamespace(panel=SimpleNamespace()),
        )

        async def original(partition_id, user_code="", silent_disarming=False):
            await asyncio.sleep(0)
            user_id = self.controller.panel.check_user(user_code)
            if user_id == -1:
                raise auth.QolsysUserCodeError
            self.assertEqual(self.controller.panel.check_user(user_code), -1)
            self.calls.append((partition_id, user_id))
            return user_id

        async def arm(partition_id, arming_type, user_code=""):
            if self.controller.settings.check_user_code_on_arm:
                return await original(partition_id, user_code)
            self.calls.append((partition_id, 0))
            return 0

        async def load_database(database):
            pass

        self.controller.panel.load_database = load_database
        self.controller.commands.panel.disarm = original
        self.controller.commands.panel.arm = arm
        self.adapter = auth.PinAuth(self.controller, asyncio.to_thread).install()

    async def test_valid_wrong_and_missing_pin(self):
        self.assertEqual(await self.controller.commands.panel.disarm("0", PIN_A), 1)
        for pin in ("99999999", "", None):
            with self.assertRaises(auth.QolsysUserCodeError):
                await self.controller.commands.panel.disarm(
                    partition_id="0", user_code=pin
                )
        self.assertEqual(self.calls, [("0", 1)])
        self.assertEqual(self.controller.panel.check_user(PIN_A), -1)

    async def test_parallel_grants_do_not_cross_users(self):
        result = await asyncio.gather(
            self.controller.commands.panel.disarm("0", PIN_A),
            self.controller.commands.panel.disarm("1", PIN_B),
        )
        self.assertEqual(result, [1, 2])
        self.assertCountEqual(self.calls, [("0", 1), ("1", 2)])

    async def test_grant_is_bound_to_its_creating_task(self):
        grant = auth._Grant("test-token", 1, asyncio.current_task())
        context = self.adapter._grant.set(grant)
        try:

            async def child():
                return self.controller.panel.check_user("test-token")

            self.assertEqual(await asyncio.create_task(child()), -1)
            self.assertEqual(self.controller.panel.check_user("test-token"), 1)
            self.assertEqual(self.controller.panel.check_user("test-token"), -1)
        finally:
            self.adapter._grant.reset(context)

    async def test_cancellation_does_not_release_kdf_lock_early(self):
        import threading

        started = threading.Event()
        release = threading.Event()

        def blocked_verify(*args):
            started.set()
            release.wait(timeout=2)
            return 1

        with patch.object(auth, "verify_pin", blocked_verify):
            task = asyncio.create_task(
                self.controller.commands.panel.disarm("0", PIN_A)
            )
            await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            self.assertTrue(self.adapter._lock.locked())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.calls)
        self.assertFalse(self.adapter._lock.locked())

    async def test_code_requirement_flags_preserved(self):
        self.assertEqual(await self.controller.commands.panel.arm("0", "STAY"), 0)
        self.controller.settings.check_user_code_on_arm = True
        with self.assertRaises(auth.QolsysUserCodeError):
            await self.controller.commands.panel.arm("0", "STAY")
        self.assertEqual(
            await self.controller.commands.panel.arm("0", "STAY", PIN_B), 2
        )
        self.assertTrue(self.controller.settings.check_user_code_on_disarm)

    async def test_online_guess_limit(self):
        for _ in range(5):
            with self.assertRaises(auth.QolsysUserCodeError):
                await self.controller.commands.panel.disarm("0", "bad")
        with self.assertRaises(auth.QolsysUserCodeError):
            await self.controller.commands.panel.disarm("0", PIN_A)
        self.assertFalse(self.calls)
        self.adapter._failures = auth.deque([auth.time.monotonic() - 61] * 5)
        self.assertEqual(await self.controller.commands.panel.disarm("0", PIN_A), 1)

    async def test_event_loop_remains_responsive_and_kdf_serialized(self):
        active = 0
        peak = 0
        ticks = 0
        original_verify = auth.verify_pin

        def slow_verify(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                auth.time.sleep(0.1)
                return original_verify(*args)
            finally:
                active -= 1

        async def ticker():
            nonlocal ticks
            for _ in range(10):
                await asyncio.sleep(0.01)
                ticks += 1

        with patch.object(auth, "verify_pin", slow_verify):
            await asyncio.gather(
                self.controller.commands.panel.disarm("0", PIN_A),
                self.controller.commands.panel.disarm("0", PIN_B),
                ticker(),
            )
        self.assertEqual(peak, 1)
        self.assertEqual(ticks, 10)

    async def test_deleted_user_and_plaintext_file_fail_closed(self):
        self.database_users = [{"userid": "2", "username": "Test B"}]
        with self.assertRaises(auth.QolsysUserCodeError):
            await self.controller.commands.panel.disarm("0", PIN_A)
        auth.write_private_json(self.path, [{"id": 2, "user_code": PIN_B}])
        with self.assertRaises(auth.QolsysUserCodeError):
            await self.controller.commands.panel.disarm("0", PIN_B)
        self.assertFalse(self.calls)

    async def test_reader_drops_plaintext_sdk_users_and_exports_only_safe_metadata(
        self,
    ):
        await asyncio.to_thread(self.controller.panel.read_users_file)
        self.assertFalse(self.controller.panel._users)
        await self.controller.panel.load_database({})
        exported = self.path.with_name("pin-users.json")
        self.assertNotIn("userPin", exported.read_text())
        self.assertNotIn("NEVER EXPORT", exported.read_text())
        self.assertEqual(set(json.loads(exported.read_text())[0]), {"id", "name"})
        self.assertEqual(exported.stat().st_mode & 0o777, 0o600)


class InstalledSDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_command_builders_with_transport_replaced(self):
        # A fresh offline controller has no host, certificates, running tasks,
        # or network connection. Its entire command transport is replaced.
        from qolsys_controller import qolsys_controller
        import qolsys_controller.commands.panel as sdk_panel
        from qolsys_controller.enum_qolsys import PartitionArmingType

        sent = []

        class OfflineTransport:
            def __init__(self, controller):
                pass

            def append_ipc_request(self, request):
                self.payload = json.loads(request[0]["dataValue"])

            async def send_command(self):
                sent.append(self.payload)
                return self.payload

        with tempfile.TemporaryDirectory() as tmp:
            controller = qolsys_controller()
            controller.settings.config_directory = tmp
            controller.settings.check_user_code_on_arm = True
            controller.settings.check_user_code_on_disarm = True
            partition = SimpleNamespace(alarm_state=object(), system_status=object())
            controller._state = SimpleNamespace(partition=lambda _: partition, zones=[])
            controller.panel.db.get_users = lambda: [
                {"userid": "7", "username": "Test"}
            ]
            path = Path(controller.settings.users_file_path)
            auth.write_private_json(
                path, [{"id": 7, "user_code_hash": auth.make_hash(PIN_A)}]
            )
            adapter = auth.PinAuth(controller, asyncio.to_thread).install()
            with patch.object(sdk_panel, "MQTTCommand_Panel", OfflineTransport):
                disarm = await controller.commands.panel.disarm("0", PIN_A)
                arm = await controller.commands.panel.arm(
                    "0", PartitionArmingType.ARM_STAY, PIN_A
                )
                self.assertEqual(disarm["userID"], 7)
                self.assertEqual(arm["userID"], 7)
                for command in (controller.commands.panel.disarm,):
                    with self.assertRaises(auth.QolsysUserCodeError):
                        await command("0", "99999999")
            self.assertEqual(len(sent), 2)
            self.assertNotIn(PIN_A, json.dumps(sent))
            self.assertEqual(adapter.check_user(PIN_A), -1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
