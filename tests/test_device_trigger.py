"""Tests for Strava Webhook device triggers and sport_type filtering."""

from types import SimpleNamespace

import pytest

from custom_components.strava_webhook.const import DOMAIN, EVENT_STRAVA_WEBHOOK, SUPPORTED_SPORT_TYPES
from custom_components.strava_webhook.device_trigger import (
    CONF_ASPECT_TYPE,
    CONF_OBJECT_TYPE,
    CONF_SPORT_TYPE,
    TRIGGER_ACTIVITY_CREATED,
    TRIGGER_ACTIVITY_CREATED_OR_UPDATED,
    TRIGGER_ACTIVITY_DELETED,
    TRIGGER_ACTIVITY_UPDATED,
    TRIGGER_SCHEMA,
    TRIGGER_WEBHOOK_RECEIVED,
    async_attach_trigger,
    async_get_trigger_capabilities,
    async_get_triggers,
)


@pytest.mark.asyncio
async def test_device_triggers_listing_and_capabilities():
    from homeassistant.core import HomeAssistant

    hass = HomeAssistant()
    triggers = await async_get_triggers(hass, "dev-123")
    assert len(triggers) == 5
    types = {t["type"] for t in triggers}
    assert types == {
        TRIGGER_ACTIVITY_CREATED,
        TRIGGER_ACTIVITY_UPDATED,
        TRIGGER_ACTIVITY_CREATED_OR_UPDATED,
        TRIGGER_ACTIVITY_DELETED,
        TRIGGER_WEBHOOK_RECEIVED,
    }

    caps = await async_get_trigger_capabilities(
        hass,
        {"platform": "device", "domain": DOMAIN, "device_id": "dev-123", "type": TRIGGER_ACTIVITY_CREATED},
    )
    assert "extra_fields" in caps
    validated = TRIGGER_SCHEMA(
        {
            "platform": "device",
            "domain": DOMAIN,
            "device_id": "dev-123",
            "type": TRIGGER_ACTIVITY_CREATED,
            CONF_SPORT_TYPE: "WeightTraining",
        }
    )
    assert validated[CONF_SPORT_TYPE] == ["WeightTraining"]
    assert "WeightTraining" in SUPPORTED_SPORT_TYPES
    assert len(SUPPORTED_SPORT_TYPES) == 56


@pytest.mark.asyncio
async def test_device_trigger_filters_by_sport_type_and_forwards_full_payload():
    from homeassistant.core import HomeAssistant

    hass = HomeAssistant()
    listeners = {}

    def _fake_listen(event_type, callback):
        listeners[event_type] = callback
        return lambda: listeners.pop(event_type, None)

    hass.bus.async_listen = _fake_listen

    fired = []

    async def _action(variables, context=None):
        fired.append(variables)

    unsub = await async_attach_trigger(
        hass,
        {
            "platform": "device",
            "domain": DOMAIN,
            "device_id": "dev-123",
            "type": TRIGGER_ACTIVITY_CREATED,
            CONF_SPORT_TYPE: ["WeightTraining"],
        },
        _action,
        {"trigger_data": {"id": "gym_trigger"}},
    )

    cb = listeners[EVENT_STRAVA_WEBHOOK]

    # Non-matching sport_type -> ignored
    await cb(
        SimpleNamespace(
            data={
                "device_id": "dev-123",
                "object_type": "activity",
                "aspect_type": "create",
                "sport_type": "Run",
            },
            context=None,
        )
    )
    assert fired == []

    # Non-matching aspect_type -> ignored
    await cb(
        SimpleNamespace(
            data={
                "device_id": "dev-123",
                "object_type": "activity",
                "aspect_type": "update",
                "sport_type": "WeightTraining",
            },
            context=None,
        )
    )
    assert fired == []

    # Matching WeightTraining create -> fires with full payload
    matching_data = {
        "device_id": "dev-123",
        "object_type": "activity",
        "aspect_type": "create",
        "sport_type": "WeightTraining",
        "name": "Morning Strength",
        "raw_webhook": {"object_id": 111},
    }
    await cb(SimpleNamespace(data=matching_data, context="ctx-1"))
    assert len(fired) == 1
    assert fired[0]["trigger"]["id"] == "gym_trigger"
    assert fired[0]["trigger"]["payload"] == matching_data
    assert fired[0]["trigger"]["event"].data == matching_data

    unsub()
    assert EVENT_STRAVA_WEBHOOK not in listeners


@pytest.mark.asyncio
async def test_webhook_received_trigger_supports_object_and_aspect_filters():
    from homeassistant.core import HomeAssistant

    hass = HomeAssistant()
    listeners = {}
    hass.bus.async_listen = lambda event_type, cb: listeners.__setitem__(event_type, cb) or (lambda: None)
    fired = []

    async def _action(variables, context=None):
        fired.append(variables)

    await async_attach_trigger(
        hass,
        {
            "platform": "device",
            "domain": DOMAIN,
            "device_id": "dev-123",
            "type": TRIGGER_WEBHOOK_RECEIVED,
            CONF_OBJECT_TYPE: "athlete",
            CONF_ASPECT_TYPE: ["update"],
        },
        _action,
        {"trigger_data": {}},
    )

    cb = listeners[EVENT_STRAVA_WEBHOOK]
    await cb(
        SimpleNamespace(
            data={"device_id": "dev-123", "object_type": "activity", "aspect_type": "create"},
            context=None,
        )
    )
    assert fired == []

    await cb(
        SimpleNamespace(
            data={
                "device_id": "dev-123",
                "object_type": "athlete",
                "aspect_type": "update",
                "updates": {"authorized": "false"},
            },
            context=None,
        )
    )
    assert len(fired) == 1
    assert fired[0]["trigger"]["payload"]["updates"] == {"authorized": "false"}
