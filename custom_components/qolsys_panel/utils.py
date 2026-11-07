"""Utility functions for Qolsys Panel Integration."""

from homeassistant.components import network
from homeassistant.core import HomeAssistant


async def get_local_ip(hass: HomeAssistant) -> str:
    """Get Home Assistant Local IP address."""
    local_ip = ""
    adapters = await network.async_get_adapters(hass)
    for adapter in adapters:
        # Respect the interfaces selected in Home Assistant, including VPN hosts.
        if adapter["enabled"]:
            for ip_info in adapter["ipv4"]:
                local_ip = ip_info["address"]

    return local_ip
