"""Number platform for IthoWiFi integration."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, is_fan_device
from .coordinator import IthoDeviceInfoCoordinator, IthoStatusCoordinator
from .entity import IthoEntity


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

    @property
    def native_value(self) -> float | None:
        """Return the last requested demand, never measured fan speed."""
        return self.coordinator.fan_demand_percent

    async def async_set_native_value(self, value: float) -> None:
        """Set demand using the same command path as the main fan."""
        await self.coordinator.async_set_fan_demand(value)
        await self.coordinator.async_request_refresh()
