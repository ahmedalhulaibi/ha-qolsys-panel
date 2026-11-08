"""Copy a validated Home Assistant daily forecast to the panel's weather table."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, tzinfo
from decimal import ROUND_HALF_UP, Decimal
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any
from zoneinfo import ZoneInfo

from qolsys_controller.mqtt_command import MQTTCommand

from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry, service
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .credential_storage import _read_private, check_storage_guard

WEATHER_URI = (
    "content://com.qolsys.qolsysprovider.ForecastWeatherContentProvider/weather"
)
WRITE_FIELDS = frozenset(
    {
        "high_temp",
        "low_temp",
        "day_of_week",
        "condition",
        "icon",
        "precipitation",
        "current_weather_date",
    }
)
ROW_FIELDS = WRITE_FIELDS | {"_id", "version", "opr", "partition_id"}
WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
# These icon IDs are confirmed from the panel's own saved forecasts. Other
# conditions use its generic cloud icon until their icon IDs are verified.
CONDITIONS = {
    "sunny": ("Sunny", "4"),
    "clear-night": ("Clear", "4"),
    "cloudy": ("Cloudy", "4"),
    "partlycloudy": ("Partly Cloudy", "1"),
    "fog": ("Fog", "4"),
    "rainy": ("Rain", "4"),
    "pouring": ("Heavy Rain", "4"),
    "snowy": ("Snow", "15"),
    "snowy-rainy": ("Rain and Snow", "15"),
    "hail": ("Hail", "4"),
    "lightning": ("Thunderstorms", "4"),
    "lightning-rainy": ("Thunderstorms", "4"),
    "windy": ("Windy", "4"),
    "windy-variant": ("Windy and Cloudy", "4"),
    "exceptional": ("Exceptional", "4"),
}


def validate_rows(value: Any) -> list[dict[str, str]]:
    """Accept bounded weather rows only, with unique numeric primary keys."""
    if not isinstance(value, list) or not 4 <= len(value) <= 7:
        raise ValueError("Expected four to seven existing weather rows")
    rows = []
    for row in value:
        if (
            not isinstance(row, dict)
            or not {"_id", *WRITE_FIELDS} <= row.keys()
            or not row.keys() <= ROW_FIELDS
        ):
            raise ValueError("Unexpected weather schema")
        if any(
            not isinstance(v, (str, int, float)) or len(str(v)) > 256
            for v in row.values()
        ):
            raise ValueError("Invalid weather values")
        normalized = {key: str(v) for key, v in row.items()}
        if not normalized["_id"].isdecimal():
            raise ValueError("Invalid weather row identifier")
        rows.append(normalized)
    if len({row["_id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate weather rows")
    if any(row["day_of_week"] not in WEEKDAYS for row in rows) or len(
        {row["day_of_week"] for row in rows}
    ) != len(rows):
        raise ValueError("Weather rows need unique weekday slots")
    return sorted(rows, key=lambda row: int(row["_id"]))


def fahrenheit(value: Any, unit: str) -> str:
    """Convert finite forecast temperatures to the panel's integer Fahrenheit."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError("Missing or invalid temperature")
    number = Decimal(str(value))
    if unit == "°C":
        number = number * Decimal(9) / Decimal(5) + Decimal(32)
    elif unit != "°F":
        raise ValueError("Unsupported temperature unit")
    if not Decimal(-148) <= number <= Decimal(149):
        raise ValueError("Temperature outside the supported weather range")
    return str(number.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def build_rows(
    original: list[dict[str, str]],
    forecast: Any,
    unit: str,
    now: datetime,
    zone: tzinfo,
) -> list[dict[str, str]]:
    """Require consecutive, correctly dated forecasts before changing any row."""
    rows = validate_rows(original)
    if not isinstance(forecast, list) or len(forecast) > 32:
        raise ValueError("Invalid daily forecast")
    by_day = {}
    for item in forecast:
        if not isinstance(item, dict):
            raise TypeError("Invalid forecast entry")
        stamp = dt_util.parse_datetime(item.get("datetime", ""))
        if stamp is None or stamp.tzinfo is None:
            raise ValueError("Forecast timestamp needs a timezone")
        day = stamp.astimezone(zone).date()
        if day in by_day:
            raise ValueError("Duplicate daily forecasts")
        by_day[day] = (item, stamp)
    today = now.astimezone(zone).date()
    wanted_days = {(today + timedelta(days=n)).strftime("%A") for n in range(len(rows))}
    existing_slots = {row["day_of_week"]: row for row in rows}
    free_slots = iter(row for row in rows if row["day_of_week"] not in wanted_days)
    result = []
    for offset in range(len(rows)):
        day = today + timedelta(days=offset)
        if day not in by_day:
            raise ValueError("Forecast does not cover every panel day")
        weekday = day.strftime("%A")
        # Keep weekdays on their original row IDs. Only an unused weekday slot
        # may be renamed, avoiding collisions in the panel's unique-day index.
        row = existing_slots[weekday] if weekday in existing_slots else next(free_slots)
        item, stamp = by_day[day]
        high = fahrenheit(item.get("temperature"), unit)
        low = fahrenheit(item.get("templow"), unit)
        if int(low) > int(high):
            raise ValueError("Forecast low exceeds its high")
        if item.get("condition") not in CONDITIONS:
            raise ValueError("Unsupported weather condition")
        condition, icon = CONDITIONS[item["condition"]]
        probability = item.get("precipitation_probability")
        if probability is None:
            # An empty value is unknown, never a fabricated 0% or a stale value.
            precipitation = ""
        else:
            if (
                isinstance(probability, bool)
                or not isinstance(probability, (int, float))
                or not math.isfinite(probability)
                or not 0 <= probability <= 100
            ):
                raise ValueError("Invalid precipitation probability")
            precipitation = str(round(probability))
        result.append(
            {
                **row,
                "high_temp": high,
                "low_temp": low,
                "day_of_week": day.strftime("%A"),
                "condition": condition,
                "icon": icon,
                "precipitation": precipitation,
                "current_weather_date": str(int(stamp.timestamp() * 1000)),
            }
        )
    return sorted(result, key=lambda row: int(row["_id"]))


def save_backup(root: Path, entry_id: str, value: dict[str, Any]) -> None:
    """Save bounded weather-only rollback data inside the guarded private tree."""
    check_storage_guard(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Weather storage is unavailable")
    folder = root / "weather-sync"
    folder.mkdir(mode=0o700, exist_ok=True)
    if (
        folder.is_symlink()
        or folder.stat().st_uid != os.geteuid()
        or folder.stat().st_mode & 0o777 != 0o700
    ):
        raise ValueError("Weather backup directory is not private")
    data = (json.dumps(value, indent=2) + "\n").encode()
    if len(data) > 131072 or not entry_id.isalnum():
        raise ValueError("Invalid weather backup")
    paths = [folder / (entry_id + ".json")]
    original = folder / (entry_id + "-original.json")
    if not original.exists() and not original.is_symlink():
        paths.insert(0, original)
    for path in paths:
        descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=folder)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            Path(temporary).replace(path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def load_backup(root: Path, entry_id: str) -> dict[str, Any] | None:
    """Load the private last-run receipt without following file symlinks."""
    check_storage_guard(root)
    path = root / "weather-sync" / (entry_id + ".json")
    if not path.exists() and not path.is_symlink():
        return None
    value = json.loads(_read_private(path, 131072))
    if not isinstance(value, dict) or value.get("weather_uri") != WEATHER_URI:
        raise ValueError("Invalid weather receipt")
    validate_rows(value["before"])
    return value


async def read_rows(controller: Any) -> list[dict[str, str]]:
    """Read only the fixed weather provider through the existing connection."""
    response = await controller.commands.panel.database_remote_read(WEATHER_URI)
    return validate_rows(response.get("resultSet"))


async def write_row(controller: Any, row: dict[str, str]) -> None:
    """Update only known weather fields on one numeric weather primary key."""
    command = MQTTCommand(controller, "database")
    command.append("dbOperation", "update")
    command.append("uri", WEATHER_URI)
    command.append("selection", "_id=" + str(int(row["_id"])))
    command.append("contentValues", {key: row[key] for key in WRITE_FIELDS})
    response = await command.send_command()
    if response.get("booleanValue") not in (True, "true"):
        raise ValueError("Panel rejected a weather row update")


async def replace_rows(controller: Any, desired: list[dict[str, str]]) -> int:
    """Update changed rows only and verify the full resulting weather table."""
    desired = validate_rows(desired)
    current = await read_rows(controller)
    if [row["_id"] for row in current] != [row["_id"] for row in desired]:
        raise ValueError("Panel weather rows changed during synchronization")
    changed = 0
    for index, (old, new) in enumerate(zip(current, desired, strict=True)):
        if old != new:
            await write_row(controller, new)
            current[index] = new
            if await read_rows(controller) != current:
                raise ValueError("Panel did not retain the expected weather row")
            changed += 1
    if await read_rows(controller) != desired:
        raise ValueError("Panel did not retain the expected forecast")
    return changed


def daily_forecast(response: Any, source: str) -> Any:
    """Validate the service response envelope before using its forecast."""
    provider = response.get(source) if isinstance(response, dict) else None
    if not isinstance(provider, dict):
        raise TypeError("Daily forecast response is unavailable")
    return provider.get("forecast")


async def async_sync_weather(call: ServiceCall) -> dict[str, Any]:
    """Synchronize a fresh external daily forecast, with verified rollback."""
    hass: HomeAssistant = call.hass
    entry = service.async_get_config_entry(
        hass, DOMAIN, call.data[ATTR_CONFIG_ENTRY_ID]
    )
    source = call.data["weather_entity"]
    state = hass.states.get(source)
    registered = entity_registry.async_get(hass).async_get(source)
    if (
        not source.startswith("weather.")
        or state is None
        or state.state in {"unknown", "unavailable"}
        or (registered and registered.platform == DOMAIN)
    ):
        raise ServiceValidationError(
            "Choose an available weather provider outside the Qolsys integration"
        )
    now = dt_util.utcnow()
    if now - state.last_reported > timedelta(hours=6):
        raise ServiceValidationError(
            "Weather source is stale; panel forecast was left unchanged"
        )
    locks = hass.data.setdefault("qolsys_weather_sync_locks", {})
    async with locks.setdefault(entry.entry_id, asyncio.Lock()):
        controller = entry.runtime_data
        root = Path(controller.settings.config_directory)
        record = None
        try:
            previous = await hass.async_add_executor_job(
                load_backup, root, entry.entry_id
            )
            if previous and previous.get("state") in {"updating", "rollback_failed"}:
                await replace_rows(controller, previous["before"])
                previous["state"] = "restored"
                await hass.async_add_executor_job(
                    save_backup, root, entry.entry_id, previous
                )
            response = await hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": source, "type": "daily"},
                blocking=True,
                return_response=True,
                context=call.context,
            )
            original = await read_rows(controller)
            desired = build_rows(
                original,
                daily_forecast(response, source),
                state.attributes.get("temperature_unit", ""),
                now,
                ZoneInfo(hass.config.time_zone),
            )
            record = {
                "weather_uri": WEATHER_URI,
                "source": source,
                "state": "updating",
                "updated_at": now.isoformat(),
                "before": original,
                "desired": desired,
            }
            await hass.async_add_executor_job(save_backup, root, entry.entry_id, record)
            changed = await replace_rows(controller, desired)
            record["state"] = "success"
            record["changed_rows"] = changed
            await hass.async_add_executor_job(save_backup, root, entry.entry_id, record)
            today_row = min(desired, key=lambda row: int(row["current_weather_date"]))
            return {
                "source": source,
                "days": len(desired),
                "changed_rows": changed,
                "updated_at": now.isoformat(),
                "today_high_fahrenheit": today_row["high_temp"],
                "today_low_fahrenheit": today_row["low_temp"],
            }
        except Exception as error:
            if record is not None:
                try:
                    await replace_rows(controller, record["before"])
                    record["state"] = "restored"
                except Exception:  # noqa: BLE001 - preserve the rollback receipt for any transport failure
                    record["state"] = "rollback_failed"
                await hass.async_add_executor_job(
                    save_backup, root, entry.entry_id, record
                )
            raise HomeAssistantError(
                "Weather synchronization failed; inspect the private weather-sync receipt"
            ) from error
