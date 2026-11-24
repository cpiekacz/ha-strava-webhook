"""Diagnostic sensor platform for Strava Webhook."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import StravaWebhookRuntime
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Strava Webhook diagnostic sensor from a config entry."""
    runtime: StravaWebhookRuntime = entry.runtime_data
    async_add_entities([StravaWebhookStatusSensor(runtime)])


class StravaWebhookStatusSensor(SensorEntity):
    """Diagnostic sensor reporting Strava webhook subscription health and stats."""

    _attr_has_entity_name = True
    _attr_translation_key = "webhook_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:webhook"

    def __init__(self, runtime: StravaWebhookRuntime) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.athlete_id}_webhook_status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(runtime.athlete_id))},
        )
        self._unsub: Any = None

    @property
    def native_value(self) -> str:
        """Return current webhook connection status."""
        return self._runtime.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return subscription and event delivery metadata."""
        last_payload = self._runtime.last_payload or {}
        return {
            "subscription_id": self._runtime.subscription_id,
            "callback_url": self._runtime.callback_url,
            "athlete_id": str(self._runtime.athlete_id),
            "events_received": self._runtime.events_received,
            "last_event_time": self._runtime.last_event_time,
            "last_object_type": last_payload.get("object_type"),
            "last_aspect_type": last_payload.get("aspect_type"),
            "last_sport_type": last_payload.get("sport_type"),
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to runtime updates."""
        await super().async_added_to_hass()
        self._unsub = self._runtime.add_listener(self._handle_runtime_update)

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from runtime updates."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_runtime_update(self, _payload: dict[str, Any]) -> None:
        """Refresh sensor state when a webhook event or resync occurs."""
        self.async_write_ha_state()
