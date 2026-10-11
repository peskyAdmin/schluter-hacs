"""Binary sensors for Schluter DITRA-HEAT thermostats."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SchluterConfigEntry
from .coordinator import SchluterCoordinator
from .entity import SchluterEntity, add_thermostat_entities

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SchluterBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], bool]
    needs_online: bool = True


DESCRIPTIONS = (
    SchluterBinarySensorDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: bool(d.get("Online")),
        needs_online=False,
    ),
    SchluterBinarySensorDescription(
        key="heating",
        translation_key="heating",
        device_class=BinarySensorDeviceClass.HEAT,
        value_fn=lambda d: bool(d.get("Heating")),
    ),
    SchluterBinarySensorDescription(
        key="error",
        translation_key="error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: bool(d.get("ErrorCode")),
        needs_online=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SchluterConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    add_thermostat_entities(
        coordinator,
        async_add_entities,
        lambda serial: [
            SchluterBinarySensor(coordinator, serial, description)
            for description in DESCRIPTIONS
        ],
    )


class SchluterBinarySensor(SchluterEntity, BinarySensorEntity):
    """A yes/no fact about one thermostat."""

    entity_description: SchluterBinarySensorDescription

    def __init__(
        self,
        coordinator: SchluterCoordinator,
        serial: str,
        description: SchluterBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, serial)
        self.entity_description = description
        self._attr_unique_id = f"{serial}_{description.key}"

    @property
    def available(self) -> bool:
        if not super().available or self._serial not in self.coordinator.data:
            return False
        return bool(self._data.get("Online")) or not self.entity_description.needs_online

    @property
    def is_on(self) -> bool:
        return self.entity_description.value_fn(self._data)
