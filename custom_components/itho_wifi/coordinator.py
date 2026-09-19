"""Data coordinator for IthoWiFi integration."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
import logging
import math
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import IthoWiFiApi, IthoWiFiApiError, IthoWiFiConnectionError, IthoWiFiNotFoundError
from .const import (
    DOMAIN,
    UPDATE_INTERVAL_DEVICEINFO,
    UPDATE_INTERVAL_REMOTES,
    UPDATE_INTERVAL_STATUS,
)

from .helpers import get_rf_demand_percent, pick_main_fan_rf_index

_LOGGER = logging.getLogger(__name__)


class IthoStatusCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for frequent status updates (speed, sensors)."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: IthoWiFiApi,
        rf_standalone: bool = False,
        rf_source_name: str | None = None,
    ) -> None:
        """Initialize the status coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_status",
            update_interval=timedelta(seconds=UPDATE_INTERVAL_STATUS),
        )
        self.api = api
        self.rf_standalone = rf_standalone
        self.rf_source_name = rf_source_name
        self.use_rf_commands = False  # set by __init__.py
        self.ota_in_progress = False
        self.remotes_coordinator: IthoRemotesCoordinator | None = None
        self.fan_demand_percent: float | None = None
        self._last_demand_command: dict[str, Any] | None = None
        self._demand_lock = asyncio.Lock()

    def rf_index(self) -> int:
        """Use the same SEND remote for both main fan controls."""
        if self.remotes_coordinator is None:
            return 0
        return pick_main_fan_rf_index(self.remotes_coordinator)

    def fan_in_auto(self) -> bool:
        """Return True only if we can confirm the unit is currently in auto mode.

        Returns False both when the unit is in a fixed mode (low/medium/high/
        timer/away/...) and when we have no FanInfo data (e.g. RF standalone
        without an rf_source configured). Used to decide whether to precede
        the 31E0 demand frame with an "auto" RF command — the unit only
        accepts demand frames when in auto mode, so we send "auto" first
        when not confirmed-auto, but skip it when confirmed-auto to avoid
        the boost-mode side-effect that ignores subsequent lower demands.
        """
        if not self.data:
            return False
        status = self.data.get("status") or {}

        # I2C 31DA path: ithostatus dict with "FanInfo" as a top-level key.
        fi = status.get("FanInfo") or status.get("fan-info")
        if fi:
            return str(fi).strip().lower() == "auto"

        # rfstatus path: sources -> measurements31DA -> {name, value}.
        for src in [status, *(status.get("sources", []) or [])]:
            for m in src.get("measurements31DA", []) or []:
                if m.get("name") in ("FanInfo", "fan-info"):
                    v = m.get("value")
                    if v is not None:
                        return str(v).strip().lower() == "auto"

        return False

    async def async_set_fan_demand(self, value: float) -> None:
        """Send and cache a requested percentage after a successful API call."""
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError("Fan demand must be between 0 and 100")
        async with self._demand_lock:
            if self.use_rf_commands:
                index = self.rf_index()
                demand = round(value * 2)
                if not self.fan_in_auto():
                    await self.api.send_rf_command("auto", index=index)
                await self.api.send_rf_demand(demand, index=index)
                value = demand / 2
            else:
                await self.api.set_speed(math.ceil(value * 2.55))
            self.fan_demand_percent = value
            # The old lastcmd must not overwrite this optimistic value.
            self.async_set_updated_data({
                **(self.data or {}),
                "fan_demand_percent": value,
            })

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch status data from the device."""
        if self.ota_in_progress:
            return self.data or {}
        async with self._demand_lock:
            return await self._async_fetch_status()

    async def _async_fetch_status(self) -> dict[str, Any]:
        """Fetch a snapshot while serialized with demand writes."""
        try:
            speed_data = await self.api.get_speed()
            lastcmd_data = await self.api.get_lastcmd()

            if self.rf_standalone and self.rf_source_name:
                status_data = await self.api.get_rfstatus(
                    name=self.rf_source_name
                )
            else:
                status_data = await self.api.get_status()

            if lastcmd_data != self._last_demand_command:
                index = self.rf_index() if self.remotes_coordinator is not None else None
                demand = get_rf_demand_percent({"lastcmd": lastcmd_data}, index=index)
                if self.use_rf_commands and demand is not None:
                    self.fan_demand_percent = demand
                self._last_demand_command = deepcopy(lastcmd_data)

            return {
                "fan_demand_percent": self.fan_demand_percent,
                "speed": speed_data,
                "status": status_data,
                "lastcmd": lastcmd_data,
            }
        except IthoWiFiConnectionError as err:
            raise UpdateFailed(f"Connection error: {err}") from err
        except IthoWiFiApiError as err:
            raise UpdateFailed(f"API error: {err}") from err


class IthoDeviceInfoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for infrequent device info updates."""

    def __init__(self, hass: HomeAssistant, api: IthoWiFiApi) -> None:
        """Initialize the device info coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_deviceinfo",
            update_interval=timedelta(seconds=UPDATE_INTERVAL_DEVICEINFO),
        )
        self.api = api
        self.ota_in_progress = False
    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch device info from the device."""
        if self.ota_in_progress:
            return self.data or {}
        try:
            return await self.api.get_deviceinfo()
        except IthoWiFiConnectionError as err:
            raise UpdateFailed(f"Connection error: {err}") from err
        except IthoWiFiApiError as err:
            raise UpdateFailed(f"API error: {err}") from err


class IthoRemotesCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for per-remote state (RF + virtual) used by per-remote fans.

    Polls /api/v2/remotes and /api/v2/vremotes and stores the result as
    {"rf": [...], "vr": [...]} with each entry being a dict with index,
    name, remtype, remtypename, remfunc, remfuncname, last_cmd, and
    isEmptySlot (computed locally).
    """

    def __init__(self, hass: HomeAssistant, api: IthoWiFiApi) -> None:
        """Initialize the remotes coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_remotes",
            update_interval=timedelta(seconds=UPDATE_INTERVAL_REMOTES),
        )
        self.api = api
        # True if the firmware exposes /api/v2/vremotes. Older firmware
        # (<3.1.0-beta3) exists but returned an empty object and doesn't
        # populate last_cmd. On 404, the coordinator keeps its last data
        # and stops polling the missing endpoint.
        self.vremotes_available: bool = True

    # Set to True by IthoFirmwareUpdate while an OTA install is in
    # progress. All coordinators check this and skip their update cycle
    # to avoid heap-exhaustion crashes on the device during download.
    ota_in_progress: bool = False
    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch both remote lists from the device."""
        if self.ota_in_progress:
            return self.data or {"rf": [], "vr": []}

        rf_list: list[dict[str, Any]] = []
        vr_list: list[dict[str, Any]] = []

        try:
            rf_list = await self.api.get_remotes()
        except IthoWiFiNotFoundError:
            rf_list = []
        except IthoWiFiConnectionError as err:
            raise UpdateFailed(f"Connection error: {err}") from err
        except IthoWiFiApiError as err:
            raise UpdateFailed(f"API error: {err}") from err

        if self.vremotes_available:
            try:
                vr_list = await self.api.get_vremotes()
            except IthoWiFiNotFoundError:
                self.vremotes_available = False
            except IthoWiFiConnectionError as err:
                raise UpdateFailed(f"Connection error: {err}") from err
            except IthoWiFiApiError:
                # Tolerate a transient vremotes failure — keep rf data.
                pass

        return {"rf": rf_list, "vr": vr_list}
