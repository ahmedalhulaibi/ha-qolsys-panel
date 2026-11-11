"""Tests for the Qolsys Panel scenes."""

from datetime import timedelta
import logging
from unittest.mock import AsyncMock, MagicMock

from conftest import PANEL_MAC
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from qolsys_controller.enum_qolsys import ControllerState

from custom_components.qolsys_panel.scene import QolsysPanelScene, async_setup_entry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform

UID = PANEL_MAC


@pytest.fixture
def controller() -> MagicMock:
    """A controller mock with one scene and an awaitable execute command."""
    c = MagicMock()
    scene = MagicMock()
    scene.scene_id = "7"
    scene.name = "Home"
    c.state.scenes = [scene]
    c.state.scene.return_value = scene
    c.controller_state = ControllerState.CONNECTED
    c.commands.panel.execute_scene = AsyncMock()
    return c


async def test_async_setup_entry_missing_unique_id_raises(
    hass: HomeAssistant, controller: MagicMock
) -> None:
    """Setup raises ConfigEntryError when the config entry has no unique_id."""
    config_entry = MagicMock()
    config_entry.runtime_data = controller
    config_entry.unique_id = None
    add_entities = MagicMock()

    with pytest.raises(ConfigEntryError):
        await async_setup_entry(hass, config_entry, add_entities)

    add_entities.assert_not_called()


async def test_async_setup_entry_creates_entities(
    hass: HomeAssistant, controller: MagicMock
) -> None:
    """Setup builds a scene entity per panel scene."""
    config_entry = MagicMock()
    config_entry.runtime_data = controller
    config_entry.unique_id = UID
    add_entities = MagicMock()

    await async_setup_entry(hass, config_entry, add_entities)

    add_entities.assert_called_once()
    entities = add_entities.call_args.args[0]
    assert len(entities) == 1
    assert isinstance(entities[0], QolsysPanelScene)


async def test_activate(controller: MagicMock) -> None:
    """Activating the scene executes it on the panel."""
    scene = QolsysPanelScene(controller, "7", UID)
    await scene.async_activate()
    controller.commands.panel.execute_scene.assert_awaited_once_with("7")


async def test_existing_scene_loses_device_prefix_without_new_id_or_activation(
    hass: HomeAssistant, controller: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Actual platform registration detaches the old device and keeps user naming."""
    mock_config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={("qolsys_panel", UID)},
        name="Alarm Panel",
    )
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "scene",
        "qolsys_panel",
        f"{UID}_scene_7",
        config_entry=mock_config_entry,
        device_id=device.id,
        suggested_object_id="panel_qolsys_panel_home",
        original_name="Qolsys Panel - Home",
    )
    registry.async_update_entity(old.entity_id, name="Arm Home Scene")
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="scene",
        platform_name="qolsys_panel",
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = mock_config_entry
    for _ in range(2):
        await platform.async_add_entities([QolsysPanelScene(controller, "7", UID)])
        await hass.async_block_till_done()
        entry = registry.async_get(old.entity_id)
        assert entry is not None
        assert entry.device_id is None
        assert entry.name == "Arm Home Scene"
        assert entry.original_name == "Home Scene"
        assert entry.unique_id == old.unique_id
        assert entry.config_entry_id == mock_config_entry.entry_id
        state = hass.states.get(old.entity_id)
        assert state is not None
        assert state.attributes["friendly_name"] == "Arm Home Scene"
        assert dr.async_get(hass).async_get(device.id) is not None
        controller.commands.panel.execute_scene.assert_not_awaited()
        await platform.async_reset()
