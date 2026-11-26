"""Service actions for the Strava Webhook integration."""

from __future__ import annotations

import time
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import (
    ASPECT_TYPES,
    CONF_CUSTOM_CALLBACK_URL,
    CONF_REPLACE_EXISTING_SUBSCRIPTION,
    DEFAULT_REPLACE_EXISTING_SUBSCRIPTION,
    DOMAIN,
    OBJECT_TYPES,
    SERVICE_RESYNC_SUBSCRIPTION,
    SERVICE_SIMULATE_EVENT,
)
from .event_builder import build_webhook_event_payload
from .runtime import (
    StravaWebhookConfigEntry,
    StravaWebhookRuntime,
    dispatch_runtime_event,
)

SIMULATE_EVENT_SCHEMA = vol.Schema(
    {
        vol.Optional("config_entry_id"): cv.string,
        vol.Optional("object_type", default="activity"): vol.In(OBJECT_TYPES),
        vol.Optional("aspect_type", default="create"): vol.In(ASPECT_TYPES),
        vol.Optional("object_id", default=999999999): vol.Coerce(int),
        vol.Optional("sport_type", default="WeightTraining"): cv.string,
        vol.Optional("name", default="Strength Training (test)"): cv.string,
        vol.Optional("updates", default=dict): dict,
        vol.Optional("extra_activity_data", default=dict): dict,
    }
)

RESYNC_SUBSCRIPTION_SCHEMA = vol.Schema(
    {
        vol.Optional("config_entry_id"): cv.string,
    }
)


def _get_target_runtimes(
    hass: HomeAssistant, config_entry_id: str | None = None
) -> list[StravaWebhookRuntime]:
    """Resolve one or all loaded StravaWebhookRuntime instances from config entries."""
    if config_entry_id:
        entry: StravaWebhookConfigEntry | None = hass.config_entries.async_get_entry(
            config_entry_id
        )
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
        ):
            raise ServiceValidationError(
                f"Strava Webhook config entry '{config_entry_id}' is not loaded"
            )
        return [entry.runtime_data]

    loaded_entries: list[StravaWebhookConfigEntry] = (
        hass.config_entries.async_loaded_entries(DOMAIN)
    )
    if not loaded_entries:
        raise ServiceValidationError("No loaded Strava Webhook integration found")
    return [entry.runtime_data for entry in loaded_entries]


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register domain-level service actions for Strava Webhook."""

    async def _handle_resync_subscription(call: ServiceCall) -> None:
        runtimes = _get_target_runtimes(hass, call.data.get("config_entry_id"))
        for runtime in runtimes:
            options = runtime.entry.options
            result = await runtime.subscription_manager.async_resync(
                custom_callback_url=options.get(CONF_CUSTOM_CALLBACK_URL),
                replace_existing=options.get(
                    CONF_REPLACE_EXISTING_SUBSCRIPTION,
                    DEFAULT_REPLACE_EXISTING_SUBSCRIPTION,
                ),
            )
            runtime.subscription_id = result["subscription_id"]
            runtime.callback_url = result["callback_url"]
            runtime.status = "connected"
            runtime.notify_listeners(runtime.last_payload or {})

    async def _handle_simulate_event(call: ServiceCall) -> None:
        runtimes = _get_target_runtimes(hass, call.data.get("config_entry_id"))
        runtime = runtimes[0]

        object_type = call.data["object_type"]
        aspect_type = call.data["aspect_type"]
        object_id = call.data["object_id"]
        sport_type = call.data["sport_type"]
        name = call.data["name"]
        updates = dict(call.data.get("updates") or {})
        extra_activity = dict(call.data.get("extra_activity_data") or {})

        raw_webhook = {
            "aspect_type": aspect_type,
            "event_time": int(time.time()),
            "object_id": object_id,
            "object_type": object_type,
            "owner_id": runtime.athlete_id,
            "subscription_id": runtime.subscription_id or 0,
            "updates": updates,
        }

        activity_data: dict[str, Any] | None = None
        enriched = False
        if object_type == "activity" and aspect_type in ("create", "update"):
            enriched = True
            activity_data = {
                "id": object_id,
                "name": name,
                "sport_type": sport_type,
                "type": sport_type,
                "distance": 0.0,
                "moving_time": 3600,
                "elapsed_time": 3700,
                "total_elevation_gain": 0.0,
                "start_date_local": dt_util.now().isoformat(),
                "trainer": False,
                "commute": False,
                "manual": False,
                "private": False,
                "device_name": "Simulated Device",
                **extra_activity,
            }

        payload = build_webhook_event_payload(
            raw_webhook,
            config_entry_id=runtime.entry.entry_id,
            device_id=runtime.device_id,
            athlete_id=runtime.athlete_id,
            activity=activity_data,
            enriched=enriched,
        )
        dispatch_runtime_event(hass, runtime, payload)

    if not hass.services.has_service(DOMAIN, SERVICE_RESYNC_SUBSCRIPTION):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RESYNC_SUBSCRIPTION,
            _handle_resync_subscription,
            schema=RESYNC_SUBSCRIPTION_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SIMULATE_EVENT):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SIMULATE_EVENT,
            _handle_simulate_event,
            schema=SIMULATE_EVENT_SCHEMA,
        )
