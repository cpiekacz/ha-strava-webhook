"""Runtime data model and event dispatch helpers for Strava Webhook."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session
from homeassistant.util import dt as dt_util

from .api import StravaApiClient
from .const import EVENT_STRAVA_WEBHOOK

if TYPE_CHECKING:
    from .subscriptions import SubscriptionManager

_LOGGER = logging.getLogger(__name__)


@dataclass
class StravaWebhookRuntime:
    """Runtime state for a configured Strava Webhook entry."""

    entry: ConfigEntry
    session: OAuth2Session
    api: StravaApiClient
    subscription_manager: SubscriptionManager
    athlete_id: int
    athlete_name: str
    device_id: str
    subscription_id: int | None = None
    callback_url: str | None = None
    status: str = "connected"
    events_received: int = 0
    last_event_time: str | None = None
    last_payload: dict[str, Any] | None = None
    _listeners: list[Callable[[dict[str, Any]], None]] = field(default_factory=list)

    @callback
    def add_listener(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Register an entity listener for incoming webhook events."""
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def notify_listeners(self, payload: dict[str, Any]) -> None:
        """Notify all attached entities of a new event or status update."""
        for listener in list(self._listeners):
            listener(payload)


StravaWebhookConfigEntry = ConfigEntry[StravaWebhookRuntime]


def dispatch_runtime_event(
    hass: HomeAssistant, runtime: StravaWebhookRuntime, payload: dict[str, Any]
) -> None:
    """Update runtime statistics, notify entities, and fire the HA bus event."""
    runtime.events_received += 1
    runtime.last_event_time = dt_util.utcnow().isoformat()
    runtime.last_payload = payload
    runtime.notify_listeners(payload)
    _LOGGER.info(
        "Dispatched %s: object_type=%s aspect_type=%s object_id=%s sport_type=%s enriched=%s",
        EVENT_STRAVA_WEBHOOK,
        payload.get("object_type"),
        payload.get("aspect_type"),
        payload.get("object_id"),
        payload.get("sport_type"),
        payload.get("enriched"),
    )
    hass.bus.async_fire(EVENT_STRAVA_WEBHOOK, payload)
