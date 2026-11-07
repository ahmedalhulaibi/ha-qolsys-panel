"""PIN enrollment through HA options uses masked fields and stores only hashes."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.qolsys_panel.const import OPTION_ARM_CODE, OPTION_DISARM_CODE
from custom_components.qolsys_panel.pin_auth import (
    load_records,
    set_user_pin,
    verify_pin,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

PIN = "0729"  # Synthetic test credentials, never a live alarm code.
OTHER_PIN = "6483"


@pytest.fixture(autouse=True)
def _zeroconf(mock_async_zeroconf: MagicMock) -> None:
    """Keep integration dependencies isolated from all network transports."""


@pytest.fixture
async def pin_entry(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
    tmp_path: Path,
):
    """Load a mocked integration with an isolated PIN store and two panel users."""
    hass.config.config_dir = str(tmp_path)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    controller = MagicMock()
    controller.settings.users_file_path = tmp_path / "qolsys_panel/users.conf"
    controller.panel.db.get_users.return_value = [
        {"userid": "1", "username": "Test owner", "userPin": "DO NOT EXPORT"},
        {"userid": "2", "username": "Test user"},
    ]
    mock_config_entry.runtime_data = controller
    return mock_config_entry, controller, controller.settings.users_file_path


async def _start_pin_form(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "pin"}
    )


def _assert_no_secret(result, entry, logs):
    rendered = json.dumps(
        {"result": result, "options": dict(entry.options), "data": dict(entry.data)},
        default=str,
    )
    assert json.dumps(PIN) not in rendered
    assert json.dumps(OTHER_PIN) not in rendered
    assert PIN not in logs
    assert OTHER_PIN not in logs
    assert "DO NOT EXPORT" not in rendered


async def test_pin_fields_are_passwords_and_never_prefilled(hass, pin_entry):
    entry, controller, path = pin_entry
    result = await _start_pin_form(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pin"
    fields = {str(key): value for key, value in result["data_schema"].schema.items()}
    assert fields["pin"].config["type"] == "password"
    assert fields["pin"].config["autocomplete"] == "new-password"
    assert fields["pin_confirm"].config == fields["pin"].config
    assert fields["panel_user"].config["options"] == [
        {"value": "1", "label": "Test owner"},
        {"value": "2", "label": "Test user"},
    ]
    assert not path.exists()
    assert controller.commands.mock_calls == []


async def test_pin_enrollment_saves_hash_without_changing_options(
    hass, pin_entry, caplog
):
    entry, controller, path = pin_entry
    options = {OPTION_ARM_CODE: False, OPTION_DISARM_CODE: True}
    hass.config_entries.async_update_entry(entry, options=options)
    result = await _start_pin_form(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"panel_user": "1", "pin": PIN, "pin_confirm": PIN}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == options
    assert result["data"] == options
    records = await hass.async_add_executor_job(load_records, path)
    assert set(records[0]) == {"id", "user_code_hash"}
    assert records[0]["user_code_hash"].startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.dumps(PIN) not in path.read_text()
    assert await hass.async_add_executor_job(verify_pin, path, PIN, frozenset({1})) == 1
    assert controller.commands.mock_calls == []
    _assert_no_secret(result, entry, caplog.text)


@pytest.mark.parametrize("invalid", ["123", "a" * 10, "１" * 4, "9" * 13])
async def test_bad_pin_does_not_create_store(hass, pin_entry, invalid, caplog):
    entry, controller, path = pin_entry
    result = await _start_pin_form(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"panel_user": "1", "pin": invalid, "pin_confirm": invalid}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"pin": "invalid_pin"}
    assert not path.exists()
    assert controller.commands.mock_calls == []
    assert json.dumps(invalid) not in json.dumps(result, default=str)
    assert invalid not in caplog.text


async def test_mismatch_clears_secret_fields(hass, pin_entry, caplog):
    entry, _, path = pin_entry
    result = await _start_pin_form(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"panel_user": "1", "pin": PIN, "pin_confirm": OTHER_PIN}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"pin_confirm": "pin_mismatch"}
    assert not path.exists()
    _assert_no_secret(result, entry, caplog.text)


async def test_user_removed_between_display_and_save_is_rejected(hass, pin_entry):
    entry, controller, path = pin_entry
    result = await _start_pin_form(hass, entry)
    controller.panel.db.get_users.return_value = [{"userid": "2", "username": "Other"}]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"panel_user": "1", "pin": PIN, "pin_confirm": PIN}
    )
    assert result["errors"] == {"panel_user": "invalid_pin_user"}
    assert not path.exists()


async def test_missing_runtime_aborts_only_pin_form(hass, pin_entry):
    entry, _, path = pin_entry
    del entry.runtime_data
    result = await _start_pin_form(hass, entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pin_panel_unavailable"
    assert not path.exists()


async def test_empty_user_list_aborts(hass, pin_entry):
    entry, controller, path = pin_entry
    controller.panel.db.get_users.return_value = []
    result = await _start_pin_form(hass, entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pin_panel_unavailable"
    assert not path.exists()


async def test_write_failure_does_not_log_or_echo_pin(hass, pin_entry, caplog):
    entry, _, path = pin_entry
    result = await _start_pin_form(hass, entry)
    with patch(
        "custom_components.qolsys_panel.config_flow.set_user_pin", side_effect=OSError
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"panel_user": "1", "pin": PIN, "pin_confirm": PIN}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_save_pin"}
    assert not path.exists()
    _assert_no_secret(result, entry, caplog.text)


async def test_update_preserves_other_users_and_refreshes_salt(hass, pin_entry):
    entry, _, path = pin_entry
    await hass.async_add_executor_job(
        set_user_pin, path, 2, OTHER_PIN, frozenset({1, 2})
    )
    second_hash = (await hass.async_add_executor_job(load_records, path))[0][
        "user_code_hash"
    ]
    first_hash = None
    for _ in range(2):
        result = await _start_pin_form(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"panel_user": "1", "pin": PIN, "pin_confirm": PIN}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        records = await hass.async_add_executor_job(load_records, path)
        hashes = {record["id"]: record["user_code_hash"] for record in records}
        assert hashes[2] == second_hash
        assert hashes[1] != first_hash
        first_hash = hashes[1]
    assert (
        await hass.async_add_executor_job(verify_pin, path, PIN, frozenset({1, 2})) == 1
    )
    assert (
        await hass.async_add_executor_job(
            verify_pin, path, OTHER_PIN, frozenset({1, 2})
        )
        == 2
    )
