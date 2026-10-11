"""Shared base for Schluter entities."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import SchluterCoordinator


class SchluterEntity(CoordinatorEntity[SchluterCoordinator]):
    """An entity tied to one thermostat, looked up by serial number."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SchluterCoordinator, serial: str) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._attr_device_info = coordinator.device_info(serial)

    @property
    def _data(self) -> dict[str, Any]:
        return self.coordinator.data.get(self._serial, {})


def add_thermostat_entities(
    coordinator: SchluterCoordinator,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[str], Iterable[Entity]],
) -> None:
    """Add entities for every thermostat now, and for any that appear later.

    A thermostat added to the account, or one that first connects to the local
    gateway after startup, gets its entities without a restart.
    """
    known: set[str] = set()
    coordinator.entity_serials.append(known)

    @callback
    def _add_new() -> None:
        new = [serial for serial in coordinator.data if serial not in known]
        if not new:
            return
        known.update(new)
        async_add_entities(entity for serial in new for entity in factory(serial))

    _add_new()
    coordinator.config_entry.async_on_unload(coordinator.async_add_listener(_add_new))
