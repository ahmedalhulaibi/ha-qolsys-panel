"""Tests for the Qolsys Panel utilities."""

from unittest.mock import AsyncMock, patch

from custom_components.qolsys_panel.utils import get_local_ip
from homeassistant.core import HomeAssistant

ADAPTERS_PATH = "custom_components.qolsys_panel.utils.network.async_get_adapters"


async def test_get_local_ip_returns_enabled_adapter(hass: HomeAssistant) -> None:
    """The selected LAN adapter wins over a disabled VPN default route."""
    adapters = [
        {"default": True, "enabled": False, "ipv4": [{"address": "100.64.0.1"}]},
        {"default": False, "enabled": True, "ipv4": [{"address": "192.168.1.9"}]},
    ]
    with patch(ADAPTERS_PATH, AsyncMock(return_value=adapters)):
        assert await get_local_ip(hass) == "192.168.1.9"


async def test_get_local_ip_no_enabled_adapter(hass: HomeAssistant) -> None:
    """With no enabled IPv4 adapter, an empty string is returned."""
    adapters = [{"default": True, "enabled": False, "ipv4": [{"address": "10.0.0.1"}]}]
    with patch(ADAPTERS_PATH, AsyncMock(return_value=adapters)):
        assert await get_local_ip(hass) == ""
