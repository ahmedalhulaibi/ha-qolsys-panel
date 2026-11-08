"""Validate weather dates, conversions, narrow writes, and rollback behavior."""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.qolsys_panel.services import async_setup_services
from custom_components.qolsys_panel.weather_sync import (
    WEATHER_URI,
    WRITE_FIELDS,
    async_sync_weather,
    build_rows,
    fahrenheit,
    load_backup,
    replace_rows,
    save_backup,
    validate_rows,
    write_row,
)
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, Unauthorized

NOW = datetime(2026, 11, 8, 3, tzinfo=UTC)
ZONE = ZoneInfo("America/Toronto")


def rows():
    """Six existing weather rows with provider metadata to preserve."""
    return [
        {
            "_id": str(6217 + n),
            "version": "0",
            "opr": "0",
            "partition_id": "0",
            "high_temp": "15",
            "low_temp": "1",
            "day_of_week": "Old day",
            "condition": "Light Snow",
            "icon": "15",
            "precipitation": "59",
            "current_weather_date": "1769551506756",
        }
        for n in range(6)
    ]


def forecast():
    """Daily provider timestamps at local noon, including the current day."""
    first = datetime(2026, 11, 7, 17, tzinfo=UTC)
    return [
        {
            "datetime": (first + timedelta(days=n)).isoformat(),
            "temperature": 9.1 + n,
            "templow": 4.1 + n,
            "condition": "cloudy",
        }
        for n in range(6)
    ]


def test_build_dates_and_preserve_metadata():
    """UTC Sunday remains Saturday locally; all six future days are replaced."""
    original = rows()
    result = build_rows(original, forecast(), "°C", NOW, ZONE)
    assert result[0]["day_of_week"] == "Saturday"
    assert result[-1]["day_of_week"] == "Thursday"
    assert result[0]["high_temp"] == "48"
    assert result[0]["low_temp"] == "39"
    assert result[0]["icon"] == "4"
    assert result[0]["precipitation"] == ""
    assert result[0]["current_weather_date"] == "1794070800000"
    assert [
        {k: row[k] for k in ("_id", "partition_id", "version", "opr")} for row in result
    ] == [
        {k: row[k] for k in ("_id", "partition_id", "version", "opr")}
        for row in original
    ]
    assert original == rows()


@pytest.mark.parametrize(
    ("value", "unit", "expected"), [(-9, "°C", "16"), (0, "°C", "32"), (15, "°F", "15")]
)
def test_temperature_conversion(value, unit, expected):
    """The panel receives integer Fahrenheit, including below-freezing values."""
    assert fahrenheit(value, unit) == expected


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf"), 1000, "9"])
def test_invalid_temperatures(value):
    """Reject missing and non-finite values instead of corrupting panel data."""
    with pytest.raises(ValueError):
        fahrenheit(value, "°C")


def test_bad_forecasts():
    """Reject incomplete/stale days, inverted ranges, and unsupported units."""
    with pytest.raises(ValueError):
        build_rows(rows(), forecast()[1:], "°C", NOW, ZONE)
    with pytest.raises(ValueError):
        build_rows(rows(), forecast(), "K", NOW, ZONE)
    broken = forecast()
    broken[0]["templow"] = 20
    with pytest.raises(ValueError):
        build_rows(rows(), broken, "°C", NOW, ZONE)
    duplicate = forecast()
    duplicate[-1] = duplicate[0]
    with pytest.raises(ValueError):
        build_rows(rows(), duplicate, "°C", NOW, ZONE)


def test_probability_and_unknown():
    """Use supplied probability and clear the old percentage when absent."""
    data = forecast()
    data[0]["precipitation_probability"] = 42
    actual = build_rows(rows(), data, "°C", NOW, ZONE)
    assert actual[0]["precipitation"] == "42"
    assert actual[1]["precipitation"] == ""
    data[0]["precipitation_probability"] = 101
    with pytest.raises(ValueError):
        build_rows(rows(), data, "°C", NOW, ZONE)


def test_schema_rejects_extra_columns_and_duplicate_ids():
    """Only the bounded weather schema may enter the write/rollback path."""
    data = rows()
    data[0]["password"] = ""
    with pytest.raises(ValueError):
        validate_rows(data)
    data = rows()
    data[1]["_id"] = data[0]["_id"]
    with pytest.raises(ValueError):
        validate_rows(data)


async def test_write_has_fixed_uri_and_fields():
    """No arbitrary table, SQL predicate, or metadata column can be written."""
    command = MagicMock()
    command.send_command = AsyncMock()
    with patch(
        "custom_components.qolsys_panel.weather_sync.MQTTCommand", return_value=command
    ):
        await write_row(MagicMock(), rows()[0])
    payload = dict(call.args for call in command.append.call_args_list)
    assert payload["uri"] == WEATHER_URI
    assert payload["selection"] == "_id=6217"
    assert payload["dbOperation"] == "update"
    assert payload["contentValues"].keys() == WRITE_FIELDS


async def test_replace_skips_unchanged_and_checks_row_ids():
    """Repeated hourly calls are idempotent; changed IDs stop before writes."""
    with (
        patch(
            "custom_components.qolsys_panel.weather_sync.read_rows",
            AsyncMock(return_value=rows()),
        ),
        patch(
            "custom_components.qolsys_panel.weather_sync.write_row", AsyncMock()
        ) as writer,
    ):
        assert await replace_rows(MagicMock(), rows()) == 0
        writer.assert_not_called()
        other = rows()
        other[0]["_id"] = "1"
        with pytest.raises(ValueError):
            await replace_rows(MagicMock(), other)
        writer.assert_not_called()


def test_private_backup_and_original_retained(tmp_path: Path):
    """Private receipts retain the initial original and reject symlinked reads."""
    value = {"weather_uri": WEATHER_URI, "before": rows(), "state": "updating"}
    save_backup(tmp_path, "entry123", value)
    value["state"] = "success"
    save_backup(tmp_path, "entry123", value)
    loaded = load_backup(tmp_path, "entry123")
    assert loaded is not None and loaded["state"] == "success"
    original = tmp_path / "weather-sync/entry123-original.json"
    assert json.loads(original.read_text())["state"] == "updating"
    assert original.stat().st_mode & 0o777 == 0o600
    assert original.parent.stat().st_mode & 0o777 == 0o700


async def test_partial_failure_rolls_back(hass: HomeAssistant, tmp_path: Path):
    """A failed batch restores the prior complete weather table."""
    hass.config.time_zone = "America/Toronto"
    hass.states.async_set("weather.source", "cloudy", {"temperature_unit": "°C"})
    controller = SimpleNamespace(
        settings=SimpleNamespace(config_directory=str(tmp_path))
    )
    entry = SimpleNamespace(entry_id="entry123", runtime_data=controller)
    replace = AsyncMock(side_effect=[ValueError("write refused"), 6])
    with (
        patch(
            "custom_components.qolsys_panel.weather_sync.service.async_get_config_entry",
            return_value=entry,
        ),
        patch(
            "custom_components.qolsys_panel.weather_sync.dt_util.utcnow",
            return_value=NOW,
        ),
        patch(
            "custom_components.qolsys_panel.weather_sync.read_rows",
            AsyncMock(return_value=rows()),
        ),
        patch("custom_components.qolsys_panel.weather_sync.replace_rows", replace),
        patch.object(
            type(hass.services),
            "async_call",
            AsyncMock(return_value={"weather.source": {"forecast": forecast()}}),
        ),
    ):
        call = ServiceCall(
            hass,
            "qolsys_panel",
            "sync_weather",
            {"config_entry_id": "entry123", "weather_entity": "weather.source"},
        )
        with pytest.raises(HomeAssistantError):
            await async_sync_weather(call)
    assert replace.await_args_list[1].args[1] == rows()
    loaded = load_backup(tmp_path, "entry123")
    assert loaded is not None and loaded["state"] == "restored"


async def test_service_requires_administrator(hass: HomeAssistant, hass_admin_user):
    """An ordinary HA user cannot invoke the weather database writer."""
    async_setup_services(hass)
    user = await hass.auth.async_create_user(
        "Weather test user", group_ids=["system-users"]
    )
    assert not user.is_admin
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            "qolsys_panel",
            "sync_weather",
            {"config_entry_id": "entry123", "weather_entity": "weather.source"},
            blocking=True,
            context=Context(user_id=user.id),
        )
