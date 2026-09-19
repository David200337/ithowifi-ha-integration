"""Number platform for IthoWiFi integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, is_fan_device
from .coordinator import IthoDeviceInfoCoordinator, IthoStatusCoordinator
from .entity import IthoEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up IthoWiFi number entities for fan demand."""
    data = hass.data[DOMAIN][entry.entry_id]
    status_coord: IthoStatusCoordinator = data["status_coordinator"]
    device_coord: IthoDeviceInfoCoordinator = data["device_coordinator"]

    devtype = (device_coord.data or {}).get("itho_devtype")
    if not is_fan_device(devtype):
        # Heatpump / AutoTemp / DemandFlow devices have no fan demand.
        return

    entities: list[NumberEntity] = [
        IthoFanDemandNumber(status_coord, device_coord),
    ]

    async_add_entities(entities)


class IthoFanDemandNumber(IthoEntity, NumberEntity):
    """Number entity for setting fan demand percentage."""

    _attr_name = "Fan demand"
    _attr_icon = "mdi:fan"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: IthoStatusCoordinator,
        device_info_coordinator: IthoDeviceInfoCoordinator,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator, device_info_coordinator)

        info = device_info_coordinator.data or {}
        self._attr_unique_id = (
            f"{info.get('add-on_hwid', 'itho')}_fan_demand"
        )

        self._last_demand: float | None = None

    @property
    def native_value(self) -> float | None:
        """Return the current fan speed as percentage."""
        if self.coordinator.data is None:
            return self._last_demand

        lastcmd = self.coordinator.data.get("lastcmd", {})
        command = str(lastcmd.get("command", ""))

        match = re.search(r"\brfdemand:(\d+)", command)

        if match:
            demand = max(0, min(int(match.group(1)), 200))
            self._last_demand = demand / 2
            return self._last_demand

        return self._last_demand

    async def async_set_native_value(self, value: float) -> None:
        """Set the fan demand. Tries RF demand first, falls back to speed."""
        try:
            await self.coordinator.api.send_rf_command("auto")

            demand = int(value * 2)  # 0-100% → 0-200 demand
            await self.coordinator.api.send_rf_demand(demand)

            self._last_demand = value
            self.async_write_ha_state()

        except Exception:
            import math
            speed = math.ceil(value * 2.55)
            await self.coordinator.api.set_speed(speed)

        await self.coordinator.async_request_refresh()
