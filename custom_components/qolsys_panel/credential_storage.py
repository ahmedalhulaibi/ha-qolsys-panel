"""Keep Qolsys credential storage private throughout setup and pairing."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
from typing import TYPE_CHECKING

from qolsys_controller.errors import QolsysConfigError

if TYPE_CHECKING:
    from qolsys_controller.controller import QolsysController

    from homeassistant.core import HomeAssistant


class CredentialPermissionsError(QolsysConfigError):
    """Credential storage could not be made private; fail setup closed."""


class CredentialStorageLockedError(QolsysConfigError):
    """An explicitly required encrypted volume is not available."""


def _read_private(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > limit
        ):
            raise CredentialPermissionsError("Invalid storage guard permissions.")
        return os.read(fd, limit + 1)
    finally:
        os.close(fd)


def check_storage_guard(root: Path) -> None:
    """Refuse plaintext fallback when a host deployment requires a mounted volume.

    The optional guard is outside the encrypted tree so it remains visible while
    locked. This checks deployment readiness, not the encryption algorithm.
    """
    guard_path = root.parent / ".qolsys-storage.json"
    if not guard_path.exists() and not guard_path.is_symlink():
        return
    try:
        guard = json.loads(_read_private(guard_path, 4096))
        if (
            not isinstance(guard, dict)
            or guard.get("version") != 1
            or guard.get("require_mount") is not True
            or not isinstance(guard.get("volume_id"), str)
            or re.fullmatch(r"[0-9a-f]{32}", guard["volume_id"]) is None
        ):
            raise CredentialPermissionsError("Invalid Qolsys storage guard.")
        if not os.path.ismount(root):
            raise CredentialStorageLockedError(
                "Encrypted Qolsys storage is locked or not mounted."
            )
        marker = root / ".qolsys-volume-id"
        if _read_private(marker, 64).decode("ascii").strip() != guard["volume_id"]:
            raise CredentialStorageLockedError(
                "Qolsys credential volume does not match its guard."
            )
    except (OSError, ValueError):
        raise CredentialPermissionsError(
            "Cannot validate Qolsys storage guard."
        ) from None


def _secure_path(path: Path, *, directory: bool) -> None:
    """Use descriptors to refuse symlinks and unexpected owners or file types."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not valid or info.st_uid != os.geteuid():
            raise CredentialPermissionsError(
                "Unexpected credential file type or owner."
            )
        if not directory and info.st_nlink != 1:
            raise CredentialPermissionsError(
                "Hard-linked credential files are refused."
            )
        os.fchmod(fd, 0o700 if directory else 0o600)
    finally:
        os.close(fd)


def harden_credentials(root: Path) -> None:
    """Restrict the Qolsys data tree, without changing key or certificate bytes."""
    check_storage_guard(root)
    try:
        if root.is_symlink():
            raise CredentialPermissionsError("Symlinked credential storage is refused.")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _secure_path(root, directory=True)
        pki = root / "pki"
        pki.mkdir(mode=0o700, exist_ok=True)
        for directory, directories, files in os.walk(root, followlinks=False):
            current = Path(directory)
            _secure_path(current, directory=True)
            for name in directories:
                _secure_path(current / name, directory=True)
            for name in files:
                _secure_path(current / name, directory=False)
    except OSError:
        raise CredentialPermissionsError(
            "Cannot secure Qolsys credential storage."
        ) from None


def install_pki_hardening(controller: QolsysController, hass: HomeAssistant) -> None:
    """Enforce private storage before SDK setup and after credential creation."""
    if getattr(controller, "_qolsys_permissions_installed", False) is True:
        return
    root = Path(controller.settings.config_directory)
    original_config = controller.config_task
    original_create = controller._pki.create

    async def config_task(start_pairing: bool) -> None:
        await hass.async_add_executor_job(harden_credentials, root)
        try:
            await original_config(start_pairing)
        finally:
            # Includes certificates received from the panel during pairing,
            # including an interrupted or unsuccessful pairing attempt.
            await hass.async_add_executor_job(harden_credentials, root)

    async def create(mac: str, key_size: int) -> bool:
        # The private ancestor exists before the SDK creates any 0755/0644
        # descendants, so they cannot become readable during key generation.
        await hass.async_add_executor_job(harden_credentials, root)
        try:
            return await original_create(mac, key_size)
        finally:
            await hass.async_add_executor_job(harden_credentials, root)

    controller.config_task = config_task  # type: ignore[method-assign]
    controller._pki.create = create  # type: ignore[method-assign]
    setattr(controller, "_qolsys_permissions_installed", True)
