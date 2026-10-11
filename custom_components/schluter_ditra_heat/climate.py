"""Climate entities for Schluter DITRA-HEAT thermostats."""

from __future__ import annotations

from typing import Any

import aiohttp

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SchluterConfigEntry
from .api import SchluterError, requested_temperature
from .const import (
    DOMAIN,
    MODE_MANUAL,
    MODE_SCHEDULE,
    MODE_VACATION,
    OFF_TEMPERATURE,
)
from .coordinator import SchluterCoordinator
from .entity import SchluterEntity, add_thermostat_entities

# The cloud handles one change at a time; don't race it.
PARALLEL_UPDATES = 1


def _to_c(value: int | None) -> float | None:
    return None if value is None else value / 100


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SchluterConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    add_thermostat_entities(
        coordinator,
        async_add_entities,
        lambda serial: [SchluterThermostat(coordinator, serial)],
    )


class SchluterThermostat(SchluterEntity, ClimateEntity):
    """A DITRA-HEAT-E-WiFi floor thermostat.

    AUTO follows the thermostat's schedule, HEAT holds a temperature
    permanently, and OFF uses the web app's away setting (hold at 5 °C).
    """

    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_hvac_modes = [HVACMode.AUTO, HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: SchluterCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = serial

    @property
    def available(self) -> bool:
        return super().available and bool(self._data.get("Online"))

    @property
    def current_temperature(self) -> float | None:
        return _to_c(self._data.get("Temperature"))

    @property
    def target_temperature(self) -> float | None:
        return _to_c(requested_temperature(self._data))

    @property
    def min_temp(self) -> float:
        return _to_c(self._data.get("MinTemp")) or 5.0

    @property
    def max_temp(self) -> float:
        return _to_c(self._data.get("MaxTemp")) or 40.0

    @property
    def hvac_mode(self) -> HVACMode:
        data = self._data
        mode = data.get("RegulationMode")
        if mode == MODE_SCHEDULE:
            return HVACMode.AUTO
        if mode == MODE_MANUAL and data.get("ManualTemperature", 0) <= OFF_TEMPERATURE:
            return HVACMode.OFF
        return HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction:
        if self.hvac_mode == HVACMode.OFF and not self._data.get("Heating"):
            return HVACAction.OFF
        return HVACAction.HEATING if self._data.get("Heating") else HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._data
        return {
            "regulation_mode": data.get("RegulationMode"),
            "vacation_enabled": data.get("VacationEnabled"),
            "early_start_of_heating": data.get("EarlyStartOfHeating"),
            "load_measured_watt": data.get("LoadMeasuredWatt"),
            "error_code": data.get("ErrorCode"),
        }

    def _vacation_flag(self) -> bool:
        # The web app clears VacationEnabled when leaving vacation mode.
        data = self._data
        return bool(data.get("VacationEnabled")) and data.get("RegulationMode") != MODE_VACATION

    async def _send(self, payload: dict[str, Any]) -> None:
        payload["VacationEnabled"] = self._vacation_flag()
        try:
            await self.coordinator.api.update_thermostat(self._serial, payload)
        except (SchluterError, aiohttp.ClientError, TimeoutError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        # Reflect the change right away; the push notification confirms it.
        data = dict(self.coordinator.data)
        data[self._serial] = {**self._data, **payload}
        self.coordinator.async_set_updated_data(data)
        if not self.coordinator.push_connected:
            await self.coordinator.async_request_refresh()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        value = round(float(temp) * 100)
        value = max(self._data.get("MinTemp", 500), min(self._data.get("MaxTemp", 4000), value))
        await self._send({"RegulationMode": MODE_MANUAL, "ManualTemperature": value})

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.AUTO:
            await self._send({"RegulationMode": MODE_SCHEDULE})
        elif hvac_mode == HVACMode.OFF:
            await self._send(
                {"RegulationMode": MODE_MANUAL, "ManualTemperature": OFF_TEMPERATURE}
            )
        else:
            temp = requested_temperature(self._data)
            if not temp or temp <= OFF_TEMPERATURE:
                temp = self._data.get("ComfortTemperature", 2000)
            await self._send({"RegulationMode": MODE_MANUAL, "ManualTemperature": temp})

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.AUTO)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
