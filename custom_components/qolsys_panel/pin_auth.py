"""Local Argon2id PIN storage and asynchronous Qolsys SDK authentication.

The panel accepts a user ID, not a PIN. Keep the verifier here and grant the
SDK a single, task-bound authentication token after successful verification.
Never put a plaintext PIN in the SDK's users list.
"""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Any, TypedDict, cast

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from qolsys_controller.errors import QolsysUserCodeError

if TYPE_CHECKING:
    from qolsys_controller.controller import QolsysController

    from homeassistant.core import HomeAssistant

_PIN = re.compile(r"[0-9]{4,12}\Z", re.ASCII)
_PHC = re.compile(
    r"\$argon2id\$v=19\$m=65536,t=3,p=4\$([A-Za-z0-9+/]{43})\$([A-Za-z0-9+/]{43})\Z"
)
_MAX_FILE_BYTES = 65536
_MAX_USERS = 128
_ENROLL_LOCK = threading.Lock()


class PinStoreError(ValueError):
    """Credential storage is unsafe or malformed; never include file contents."""


class PinRecord(TypedDict):
    """A panel user ID and its PHC verifier, with no plaintext field."""

    id: int
    user_code_hash: str


class PanelUser(TypedDict):
    """Non-secret user metadata for the enrollment helper."""

    id: int
    name: str


def make_hash(pin: str) -> str:
    """Derive a PHC verifier with a fresh 256-bit random salt."""
    if not is_valid_pin(pin):
        raise PinStoreError("Use a separate Home Assistant PIN of 4 to 12 digits.")
    return Argon2id(
        salt=secrets.token_bytes(32),
        length=32,
        iterations=3,
        lanes=4,
        memory_cost=65536,
    ).derive_phc_encoded(pin.encode("ascii"))


def is_valid_pin(pin: object) -> bool:
    """Check the enrollment policy without hashing or retaining the PIN."""
    return isinstance(pin, str) and _PIN.fullmatch(pin) is not None


def _valid_hash(value: object) -> bool:
    if not isinstance(value, str) or (match := _PHC.fullmatch(value)) is None:
        return False
    # PHC uses unpadded base64. Require canonical 32-byte salt and hash values.
    return all(
        base64.b64encode(base64.b64decode(part + "=", validate=True))
        .decode("ascii")
        .rstrip("=")
        == part
        for part in match.groups()
    )


def validate_records(records: object) -> list[PinRecord]:
    if not isinstance(records, list) or len(records) > _MAX_USERS:
        raise PinStoreError("Invalid PIN store.")
    seen = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"id", "user_code_hash"}:
            raise PinStoreError(
                "PIN store must contain hashes only; plaintext is refused."
            )
        user_id = record["id"]
        if type(user_id) is not int or not 1 <= user_id <= 9999 or user_id in seen:
            raise PinStoreError("Invalid or duplicate panel user ID.")
        if not _valid_hash(record["user_code_hash"]):
            raise PinStoreError("Unsupported or malformed PIN hash.")
        seen.add(user_id)
    return cast(list[PinRecord], records)


def load_records(path: Path) -> list[PinRecord]:
    """Read an owner-only regular file, refusing symlinks and plaintext."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return []
    except OSError:
        raise PinStoreError("Cannot safely open PIN store.") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or info.st_size > _MAX_FILE_BYTES
        ):
            raise PinStoreError("PIN store must be an owner-only regular file (0600).")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            records = json.loads(stream.read(_MAX_FILE_BYTES + 1))
        return validate_records(records)
    except (UnicodeError, json.JSONDecodeError):
        raise PinStoreError("Invalid PIN store.") from None
    finally:
        if fd != -1:
            os.close(fd)


def write_private_json(path: Path, value: object) -> None:
    """Replace a private JSON file atomically, without plaintext backup files."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise PinStoreError("Refusing a symlink destination.")
    fd, temporary = tempfile.mkstemp(prefix=".pin-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def verify_pin(path: Path, pin: str, allowed_ids: frozenset[int]) -> int:
    """Blocking verifier: called only in an executor or the private CLI."""
    if not isinstance(pin, str) or not _PIN.fullmatch(pin):
        return -1
    for record in load_records(path):
        if record["id"] not in allowed_ids:
            continue
        try:
            Argon2id.verify_phc_encoded(pin.encode("ascii"), record["user_code_hash"])
        except InvalidKey:
            continue
        return record["id"]
    return -1


def set_user_pin(
    path: Path, user_id: int, pin: str, allowed_ids: frozenset[int]
) -> None:
    """Enroll a verifier in an executor, preserving all other user records."""
    if type(user_id) is not int or user_id not in allowed_ids:
        raise PinStoreError("Choose an existing panel user.")
    # Serialize enrollment from simultaneous options flows in this process.
    with _ENROLL_LOCK:
        records = load_records(path)
        encoded = make_hash(pin)
        records = [record for record in records if record["id"] != user_id]
        records.append({"id": user_id, "user_code_hash": encoded})
        validate_records(records)
        write_private_json(path, records)


def panel_users(controller: QolsysController) -> list[PanelUser]:
    """Export IDs and display names only; never export the panel's PIN fields."""
    result: dict[int, PanelUser] = {}
    for user in controller.panel.db.get_users():
        try:
            user_id = int(user.get("userid", ""))
        except (ValueError, TypeError):
            continue
        if 1 <= user_id <= 9999:
            result[user_id] = {
                "id": user_id,
                "name": str(user.get("username", ""))[:80],
            }
    return [result[key] for key in sorted(result)]


@dataclass
class _Grant:
    token: str
    user_id: int
    task: object
    consumed: bool = False


class PinAuth:
    """Adapt SDK 1.12.7 without changing its installed package or code flags."""

    def __init__(
        self,
        controller: QolsysController,
        executor: Callable[..., Awaitable[Any]],
    ) -> None:
        self.controller = controller
        self.executor = executor
        self.path = Path(controller.settings.users_file_path)
        self._grant: ContextVar[_Grant | None] = ContextVar(
            "qolsys_pin_grant", default=None
        )
        self._lock = asyncio.Lock()
        self._failures: deque[float] = deque()

    def check_user(self, user_code: str) -> int:
        """Only accept a one-use grant in the task that authenticated it."""
        grant = self._grant.get()
        if (
            grant is None
            or grant.consumed
            or grant.task is not asyncio.current_task()
            or not isinstance(user_code, str)
            or not secrets.compare_digest(user_code, grant.token)
        ):
            return -1
        grant.consumed = True
        return grant.user_id

    async def _authenticate(self, pin: str) -> int:
        # Bound both online guessing and concurrent 64 MiB KDF allocations.
        async with self._lock:
            now = time.monotonic()
            while self._failures and now - self._failures[0] >= 60:
                self._failures.popleft()
            if len(self._failures) >= 5:
                raise QolsysUserCodeError
            allowed = frozenset(user["id"] for user in panel_users(self.controller))
            try:
                job = asyncio.ensure_future(
                    self.executor(verify_pin, self.path, pin, allowed)
                )
                try:
                    user_id = cast(int, await asyncio.shield(job))
                except asyncio.CancelledError:
                    # Cancelling an await cannot stop the executor's KDF. Keep
                    # the memory allocation serialized until it has finished.
                    try:
                        await job
                    finally:
                        raise
            except (OSError, PinStoreError):
                user_id = -1
            if user_id == -1:
                self._failures.append(time.monotonic())
                raise QolsysUserCodeError
            return user_id

    def _wrap(
        self,
        original: Callable[..., Awaitable[Any]],
        required_setting: str,
    ) -> Callable[..., Awaitable[Any]]:
        signature = inspect.signature(original)
        if "user_code" not in signature.parameters:
            raise PinStoreError("Unsupported Qolsys SDK command signature.")

        @wraps(original)
        async def command(*args: Any, **kwargs: Any) -> Any:
            if not getattr(self.controller.settings, required_setting):
                return await original(*args, **kwargs)
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            user_id = await self._authenticate(bound.arguments["user_code"])
            token = secrets.token_hex(32)
            bound.arguments["user_code"] = token
            context = self._grant.set(_Grant(token, user_id, asyncio.current_task()))
            try:
                return await original(*bound.args, **bound.kwargs)
            finally:
                self._grant.reset(context)

        return command

    def install(self) -> PinAuth:
        controller = self.controller

        # Replace the plaintext reader before the controller starts. The SDK
        # invokes this reader in an executor on each connection attempt.
        def read_users_file() -> None:
            controller.panel._users.clear()
            load_records(self.path)

        controller.panel.read_users_file = read_users_file  # type: ignore[method-assign]
        controller.panel.check_user = self.check_user  # type: ignore[method-assign]
        for name in ("arm", "disarm"):
            original = getattr(controller.commands.panel, name)
            setattr(
                controller.commands.panel,
                name,
                self._wrap(original, f"check_user_code_on_{name}"),
            )

        original_load = controller.panel.load_database

        @wraps(original_load)
        async def load_database(*args: Any, **kwargs: Any) -> None:
            await original_load(*args, **kwargs)
            users = panel_users(controller)
            await self.executor(
                write_private_json, self.path.with_name("pin-users.json"), users
            )

        controller.panel.load_database = load_database  # type: ignore[method-assign]
        return self


def install_pin_auth(controller: QolsysController, hass: HomeAssistant) -> PinAuth:
    """Install before run_forever; all KDF work stays off the HA event loop."""
    return PinAuth(controller, hass.async_add_executor_job).install()
