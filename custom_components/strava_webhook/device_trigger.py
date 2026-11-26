"""Device automation triggers for Strava Webhook."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import (
    ASPECT_TYPES,
    DOMAIN,
    EVENT_STRAVA_WEBHOOK,
    OBJECT_TYPES,
    SUPPORTED_SPORT_TYPES,
)

TRIGGER_ACTIVITY_CREATED = "activity_created"
TRIGGER_ACTIVITY_UPDATED = "activity_updated"
TRIGGER_ACTIVITY_DELETED = "activity_deleted"
TRIGGER_ACTIVITY_CREATED_OR_UPDATED = "activity_created_or_updated"
TRIGGER_WEBHOOK_RECEIVED = "webhook_received"

TRIGGER_TYPES: tuple[str, ...] = (
    TRIGGER_ACTIVITY_CREATED,
    TRIGGER_ACTIVITY_UPDATED,
    TRIGGER_ACTIVITY_CREATED_OR_UPDATED,
    TRIGGER_ACTIVITY_DELETED,
    TRIGGER_WEBHOOK_RECEIVED,
)

CONF_SPORT_TYPE = "sport_type"
CONF_OBJECT_TYPE = "object_type"
CONF_ASPECT_TYPE = "aspect_type"


def _ensure_str_list(value: Any) -> list[str]:
    """Accept either a single string or a list of strings in YAML/UI configs."""
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise vol.Invalid("Expected a string or list of strings")


TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES),
        vol.Optional(CONF_SPORT_TYPE): vol.All(_ensure_str_list, [cv.string]),
        vol.Optional(CONF_OBJECT_TYPE): vol.In(OBJECT_TYPES),
        vol.Optional(CONF_ASPECT_TYPE): vol.All(
            _ensure_str_list, [vol.In(ASPECT_TYPES)]
        ),
    }
)


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """List device triggers exposed in the Automation UI for a Strava device."""
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: trigger_type,
        }
        for trigger_type in TRIGGER_TYPES
    ]


async def async_get_trigger_capabilities(
    hass: HomeAssistant, config: ConfigType
) -> dict[str, vol.Schema]:
    """List optional filter fields shown in the Automation UI."""
    trigger_type = config.get(CONF_TYPE)
    sport_selector = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[sport.lower() for sport in SUPPORTED_SPORT_TYPES],
            multiple=True,
            custom_value=True,
            translation_key="sport_type",
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )

    if trigger_type in (
        TRIGGER_ACTIVITY_CREATED,
        TRIGGER_ACTIVITY_UPDATED,
        TRIGGER_ACTIVITY_CREATED_OR_UPDATED,
    ):
        return {
            "extra_fields": vol.Schema(
                {
                    vol.Optional(CONF_SPORT_TYPE): sport_selector,
                }
            )
        }

    if trigger_type == TRIGGER_WEBHOOK_RECEIVED:
        return {
            "extra_fields": vol.Schema(
                {
                    vol.Optional(CONF_OBJECT_TYPE): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(OBJECT_TYPES),
                            multiple=False,
                            translation_key="object_type",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(CONF_ASPECT_TYPE): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(ASPECT_TYPES),
                            multiple=True,
                            translation_key="aspect_type",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(CONF_SPORT_TYPE): sport_selector,
                }
            )
        }

    return {"extra_fields": vol.Schema({})}


def _matches_sport_filter(
    event_data: dict[str, Any], sport_filters: list[str]
) -> bool:
    """Return True if sport_filters is empty or matches activity sport_type / type."""
    if not sport_filters:
        return True
    normalized_filters = {item.lower() for item in sport_filters}
    candidates = [
        event_data.get("sport_type"),
        event_data.get("type"),
    ]
    updates = event_data.get("updates")
    if isinstance(updates, dict):
        candidates.append(updates.get("sport_type"))
        candidates.append(updates.get("type"))
    return any(
        isinstance(candidate, str) and candidate.lower() in normalized_filters
        for candidate in candidates
    )


def _matches_trigger_config(
    event_data: dict[str, Any], config: ConfigType
) -> bool:
    """Evaluate whether a strava_webhook_event matches the configured device trigger."""
    if event_data.get("device_id") != config[CONF_DEVICE_ID]:
        return False

    trigger_type = config[CONF_TYPE]
    object_type = event_data.get("object_type")
    aspect_type = event_data.get("aspect_type")

    if trigger_type == TRIGGER_ACTIVITY_CREATED:
        if object_type != "activity" or aspect_type != "create":
            return False
    elif trigger_type == TRIGGER_ACTIVITY_UPDATED:
        if object_type != "activity" or aspect_type != "update":
            return False
    elif trigger_type == TRIGGER_ACTIVITY_CREATED_OR_UPDATED:
        if object_type != "activity" or aspect_type not in ("create", "update"):
            return False
    elif trigger_type == TRIGGER_ACTIVITY_DELETED:
        if object_type != "activity" or aspect_type != "delete":
            return False
    elif trigger_type == TRIGGER_WEBHOOK_RECEIVED:
        expected_object = config.get(CONF_OBJECT_TYPE)
        if expected_object and object_type != expected_object:
            return False
        expected_aspects = _ensure_str_list(config.get(CONF_ASPECT_TYPE))
        if expected_aspects and aspect_type not in expected_aspects:
            return False

    sport_filters = _ensure_str_list(config.get(CONF_SPORT_TYPE))
    return _matches_sport_filter(event_data, sport_filters)


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a listener for Strava webhook device triggers."""
    job = HassJob(action)
    trigger_data = trigger_info["trigger_data"]
    device_id = config[CONF_DEVICE_ID]
    trigger_type = config[CONF_TYPE]

    async def _handle_event(event: Any) -> None:
        if not _matches_trigger_config(event.data, config):
            return

        payload = {
            "trigger": {
                **trigger_data,
                "platform": "device",
                "domain": DOMAIN,
                "device_id": device_id,
                "type": trigger_type,
                "description": f"Strava Webhook ({trigger_type})",
                "event": event,
                "payload": event.data,
            }
        }
        await hass.async_run_hass_job(job, payload, event.context)

    return hass.bus.async_listen(EVENT_STRAVA_WEBHOOK, _handle_event)
