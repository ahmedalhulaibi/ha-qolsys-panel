"""Private key storage permissions are enforced without contacting a panel."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from qolsys_controller import qolsys_controller

from custom_components.qolsys_panel.credential_storage import (
    CredentialPermissionsError,
    CredentialStorageLockedError,
    harden_credentials,
    install_pki_hardening,
)


def test_existing_credentials_restrict_permissions_without_changing_bytes(tmp_path):
    root = tmp_path / "qolsys_panel"
    identity = root / "pki/112233445566"
    identity.mkdir(parents=True)
    before = {}
    for suffix in ("key", "cer", "csr", "secure", "qolsys"):
        file = identity / ("112233445566." + suffix)
        file.write_bytes(b"synthetic-test-data-" + suffix.encode())
        file.chmod(0o644)
        before[file] = file.read_bytes()
    harden_credentials(root)
    for path in (root, root / "pki", identity):
        assert path.stat().st_mode & 0o777 == 0o700
    for file, data in before.items():
        assert file.stat().st_mode & 0o777 == 0o600
        assert file.read_bytes() == data


@pytest.mark.parametrize("symlink_at", ["root", "identity", "file"])
def test_symlinks_are_refused_and_target_untouched(tmp_path, symlink_at):
    root = tmp_path / "qolsys_panel"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    file = outside / "unrelated.key"
    file.write_text("synthetic-data")
    file.chmod(0o644)
    if symlink_at == "root":
        root.symlink_to(outside, target_is_directory=True)
    else:
        pki = root / "pki"
        pki.mkdir(parents=True)
        if symlink_at == "identity":
            (pki / "112233445566").symlink_to(outside, target_is_directory=True)
        else:
            (pki / "identity.key").symlink_to(file)
    with pytest.raises(CredentialPermissionsError):
        harden_credentials(root)
    assert file.stat().st_mode & 0o777 == 0o644
    assert outside.stat().st_mode & 0o777 == 0o755


def test_hardlinks_are_refused(tmp_path):
    root = tmp_path / "qolsys_panel"
    pki = root / "pki"
    pki.mkdir(parents=True)
    outside = tmp_path / "unrelated.key"
    outside.write_text("synthetic-data")
    outside.chmod(0o644)
    (pki / "linked.key").hardlink_to(outside)
    with pytest.raises(CredentialPermissionsError):
        harden_credentials(root)
    assert outside.stat().st_mode & 0o777 == 0o644


async def test_real_sdk_generates_private_credentials_in_protected_tree(hass, tmp_path):
    controller = qolsys_controller()
    root = tmp_path / "qolsys_panel"
    controller.settings.config_directory = str(root)
    install_pki_hardening(controller, hass)
    # Offline key and self-signed certificate creation; no controller run or
    # network transport is ever started.
    assert await controller._pki.create("11:22:33:44:55:66", 2048)
    identity = root / "pki/112233445566"
    assert identity.stat().st_mode & 0o777 == 0o700
    for file in identity.iterdir():
        assert file.stat().st_mode & 0o777 == 0o600


async def test_setup_runs_inside_private_root_and_hardens_received_certificate(
    hass,
    tmp_path,
):
    root = tmp_path / "qolsys_panel"

    async def original_config(start_pairing):
        assert root.stat().st_mode & 0o777 == 0o700
        assert (root / "pki").stat().st_mode & 0o777 == 0o700
        certificate = root / "pki/new.secure"
        certificate.write_text("synthetic-signed-certificate")
        certificate.chmod(0o644)
        raise RuntimeError("synthetic pairing interruption")

    controller = qolsys_controller()
    controller.settings.config_directory = str(root)
    controller.config_task = original_config
    controller._pki.create = AsyncMock(return_value=True)
    install_pki_hardening(controller, hass)
    wrapped = controller.config_task
    install_pki_hardening(controller, hass)
    assert controller.config_task is wrapped
    with pytest.raises(RuntimeError, match="synthetic pairing interruption"):
        await controller.config_task(True)
    assert (root / "pki/new.secure").stat().st_mode & 0o777 == 0o600


def _write_guard(root, *, volume_id="1" * 32):
    guard = root.parent / ".qolsys-storage.json"
    guard.write_text(
        json.dumps({"version": 1, "require_mount": True, "volume_id": volume_id})
    )
    guard.chmod(0o600)


def test_locked_volume_never_creates_plaintext_credentials(tmp_path):
    root = tmp_path / "qolsys_panel"
    _write_guard(root)
    with pytest.raises(CredentialStorageLockedError):
        harden_credentials(root)
    assert not root.exists()


def test_wrong_volume_marker_refuses_setup(tmp_path):
    root = tmp_path / "qolsys_panel"
    root.mkdir()
    marker = root / ".qolsys-volume-id"
    marker.write_text("2" * 32)
    marker.chmod(0o600)
    _write_guard(root)
    with (
        patch("os.path.ismount", return_value=True),
        pytest.raises(CredentialStorageLockedError),
    ):
        harden_credentials(root)
    assert not (root / "pki").exists()


def test_mounted_matching_volume_is_usable(tmp_path):
    root = tmp_path / "qolsys_panel"
    root.mkdir()
    marker = root / ".qolsys-volume-id"
    marker.write_text("1" * 32)
    marker.chmod(0o600)
    _write_guard(root)
    with patch("os.path.ismount", return_value=True):
        harden_credentials(root)
    assert (root / "pki").is_dir()


def test_malformed_guard_fails_closed(tmp_path):
    root = tmp_path / "qolsys_panel"
    guard = root.parent / ".qolsys-storage.json"
    guard.write_text("{}")
    guard.chmod(0o600)
    with pytest.raises(CredentialPermissionsError):
        harden_credentials(root)
    assert not root.exists()
