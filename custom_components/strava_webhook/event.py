"""Event entity platform for Strava Webhook."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import StravaWebhookRuntime
from .const import DOMAIN
from .event_builder import build_event_entity_attributes, determine_entity_event_type


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Strava Webhook event entity from a config entry."""
    runtime: StravaWebhookRuntime = entry.runtime_data
    async_add_entities([StravaWebhookEventEntity(runtime)])


class StravaWebhookEventEntity(EventEntity):
    """Represents the latest incoming Strava webhook event for an athlete."""

    _attr_has_entity_name = True
    _attr_translation_key = "last_webhook_event"
    _attr_event_types = [
        "activity_created",
        "activity_updated",
        "activity_deleted",
        "athlete_updated",
        "webhook_received",
    ]

    def __init__(self, runtime: StravaWebhookRuntime) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.athlete_id}_last_webhook_event"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(runtime.athlete_id))},
        )
        self._unsub: Any = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to runtime webhook notifications."""
        await super().async_added_to_hass()
        self._unsub = self._runtime.add_listener(self._handle_webhook_event)

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from runtime notifications."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_webhook_event(self, payload: dict[str, Any]) -> None:
        """Record the incoming Strava webhook event on the EventEntity."""
        event_type = determine_entity_event_type(
            payload.get("object_type"), payload.get("aspect_type")
        )
        attrs = build_event_entity_attributes(payload)
        self._trigger_event(event_type, attrs)
        self.async_write_ha_state()
