"""Tests for config flow, options flow, entities, and service actions."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.strava_webhook import async_setup
from custom_components.strava_webhook.api import StravaApiError, StravaAuthError
from custom_components.strava_webhook.config_flow import (
    StravaWebhookConfigFlow,
    StravaWebhookOptionsFlow,
)
from custom_components.strava_webhook.const import (
    CONF_ATHLETE_ID,
    CONF_ATHLETE_NAME,
    CONF_CLIENT_ID,
    CONF_CUSTOM_CALLBACK_URL,
    CONF_ENRICH_ACTIVITIES,
    CONF_REPLACE_EXISTING_SUBSCRIPTION,
    DOMAIN,
    EVENT_STRAVA_WEBHOOK,
    SERVICE_RESYNC_SUBSCRIPTION,
    SERVICE_SIMULATE_EVENT,
)
from custom_components.strava_webhook.runtime import StravaWebhookRuntime
from custom_components.strava_webhook.sensor import (
    WEBHOOK_STATUS_OPTIONS,
    StravaWebhookStatusSensor,
)


class _AbortFlow(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _make_config_flow(
    *,
    source: str = "user",
    already_configured: bool = False,
    reauth_entry: SimpleNamespace | None = None,
) -> StravaWebhookConfigFlow:
    flow = StravaWebhookConfigFlow()
    flow.hass = HomeAssistant()
    flow.source = source
    flow.flow_impl = SimpleNamespace(client_id="32206")
    flow._unique_id = None

    async def _set_unique_id(uid: str) -> None:
        flow._unique_id = uid

    def _abort_if_configured() -> None:
        if already_configured:
            raise _AbortFlow("already_configured")

    def _abort_if_mismatch(*, reason: str) -> None:
        if reauth_entry is not None and reauth_entry.unique_id != flow._unique_id:
            raise _AbortFlow(reason)

    flow.async_set_unique_id = _set_unique_id
    flow._abort_if_unique_id_configured = _abort_if_configured
    flow._abort_if_unique_id_mismatch = _abort_if_mismatch
    flow._get_reauth_entry = lambda: reauth_entry
    flow.async_create_entry = lambda **kwargs: {"type": "create_entry", **kwargs}
    flow.async_abort = lambda *, reason: {"type": "abort", "reason": reason}
    flow.async_show_form = lambda **kwargs: {"type": "form", **kwargs}
    flow.async_update_reload_and_abort = lambda entry, **kwargs: {
        "type": "abort",
        "reason": "reauth_successful",
        "entry": entry,
        **kwargs,
    }
    flow.async_step_pick_implementation = AsyncMock(
        return_value={"type": "external_step"}
    )
    return flow


@pytest.mark.asyncio
async def test_oauth_create_entry_happy_path_sets_unique_id():
    flow = _make_config_flow()
    with patch(
        "custom_components.strava_webhook.config_flow.async_verify_token_and_fetch_athlete",
        AsyncMock(return_value={"id": 35638399, "firstname": "Jane", "lastname": "Doe"}),
    ):
        result = await flow.async_oauth_create_entry(
            {"token": {"access_token": "tok-1"}}
        )

    assert result["type"] == "create_entry"
    assert result["title"] == "Strava Webhook (Jane Doe)"
    assert flow._unique_id == "35638399"
    assert result["data"][CONF_ATHLETE_ID] == "35638399"
    assert result["data"][CONF_ATHLETE_NAME] == "Jane Doe"
    assert result["data"][CONF_CLIENT_ID] == "32206"
    assert result["options"][CONF_ENRICH_ACTIVITIES] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected_reason"),
    [
        (StravaAuthError("missing_activity_scope", status=401), "missing_activity_scope"),
        (StravaAuthError("unauthorized", status=401), "cannot_connect"),
        (StravaApiError("boom", status=500), "cannot_connect"),
    ],
)
async def test_oauth_create_entry_errors_abort_cleanly(exc, expected_reason):
    flow = _make_config_flow()
    with patch(
        "custom_components.strava_webhook.config_flow.async_verify_token_and_fetch_athlete",
        AsyncMock(side_effect=exc),
    ):
        result = await flow.async_oauth_create_entry(
            {"token": {"access_token": "tok-1"}}
        )
    assert result == {"type": "abort", "reason": expected_reason}


@pytest.mark.asyncio
async def test_oauth_create_entry_duplicate_aborts():
    flow = _make_config_flow(already_configured=True)
    with (
        patch(
            "custom_components.strava_webhook.config_flow.async_verify_token_and_fetch_athlete",
            AsyncMock(return_value={"id": 35638399, "username": "runner"}),
        ),
        pytest.raises(_AbortFlow) as exc_info,
    ):
        await flow.async_oauth_create_entry({"token": {"access_token": "tok-1"}})

    assert exc_info.value.reason == "already_configured"


@pytest.mark.asyncio
async def test_reauth_flow_confirm_success_and_wrong_account():
    reauth_entry = SimpleNamespace(
        unique_id="35638399",
        data={"auth_implementation": "strava_webhook_32206"},
    )
    flow = _make_config_flow(source=SOURCE_REAUTH, reauth_entry=reauth_entry)

    # Step 1: initial form shown
    form = await flow.async_step_reauth({})
    assert form["type"] == "form"
    assert form["step_id"] == "reauth_confirm"

    # Step 2: user confirms -> proceeds to pick_implementation
    confirmed = await flow.async_step_reauth_confirm({})
    assert confirmed == {"type": "external_step"}
    flow.async_step_pick_implementation.assert_awaited_once_with(
        {"implementation": "strava_webhook_32206"}
    )

    # Wrong Strava account aborts with wrong_account
    with (
        patch(
            "custom_components.strava_webhook.config_flow.async_verify_token_and_fetch_athlete",
            AsyncMock(return_value={"id": 999999, "username": "someone_else"}),
        ),
        pytest.raises(_AbortFlow) as exc_info,
    ):
        await flow.async_oauth_create_entry({"token": {"access_token": "tok-wrong"}})
    assert exc_info.value.reason == "wrong_account"

    # Matching account updates & reloads entry
    with patch(
        "custom_components.strava_webhook.config_flow.async_verify_token_and_fetch_athlete",
        AsyncMock(return_value={"id": 35638399, "firstname": "Jane", "lastname": "Doe"}),
    ):
        result = await flow.async_oauth_create_entry(
            {"token": {"access_token": "tok-ok"}}
        )
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert result["data_updates"][CONF_ATHLETE_ID] == "35638399"


@pytest.mark.asyncio
async def test_options_flow_validation_error_and_recovery():
    flow = StravaWebhookOptionsFlow()
    flow.config_entry = SimpleNamespace(
        options={
            CONF_ENRICH_ACTIVITIES: True,
            CONF_CUSTOM_CALLBACK_URL: "",
            CONF_REPLACE_EXISTING_SUBSCRIPTION: False,
        }
    )
    flow.async_show_form = lambda **kwargs: {"type": "form", **kwargs}
    flow.async_create_entry = lambda **kwargs: {"type": "create_entry", **kwargs}

    # Initial form
    initial = await flow.async_step_init()
    assert initial["type"] == "form"
    assert initial["errors"] == {}

    # Invalid HTTP URL triggers validation error
    invalid = await flow.async_step_init(
        {
            CONF_ENRICH_ACTIVITIES: True,
            CONF_CUSTOM_CALLBACK_URL: "http://insecure.example.com/hook",
            CONF_REPLACE_EXISTING_SUBSCRIPTION: False,
        }
    )
    assert invalid["type"] == "form"
    assert invalid["errors"] == {CONF_CUSTOM_CALLBACK_URL: "invalid_callback_url"}

    # Recovery with valid HTTPS URL succeeds
    recovered = await flow.async_step_init(
        {
            CONF_ENRICH_ACTIVITIES: False,
            CONF_CUSTOM_CALLBACK_URL: "https://ha.example.com/api/webhook/custom",
            CONF_REPLACE_EXISTING_SUBSCRIPTION: True,
        }
    )
    assert recovered == {
        "type": "create_entry",
        "data": {
            CONF_ENRICH_ACTIVITIES: False,
            CONF_CUSTOM_CALLBACK_URL: "https://ha.example.com/api/webhook/custom",
            CONF_REPLACE_EXISTING_SUBSCRIPTION: True,
        },
    }


@pytest.mark.asyncio
async def test_status_sensor_is_push_enum_entity():
    runtime = StravaWebhookRuntime(
        entry=SimpleNamespace(entry_id="e1", options={}),
        session=MagicMock(),
        api=MagicMock(),
        subscription_manager=MagicMock(),
        athlete_id=35638399,
        athlete_name="Jane Doe",
        device_id="dev-1",
        subscription_id=123,
        callback_url="https://ha.example.com/api/webhook/wh",
    )
    sensor = StravaWebhookStatusSensor(runtime)

    assert sensor._attr_should_poll is False
    assert sensor._attr_device_class == SensorDeviceClass.ENUM
    assert sensor._attr_options == WEBHOOK_STATUS_OPTIONS
    assert sensor.native_value == "connected"

    await sensor.async_added_to_hass()
    assert len(runtime._listeners) == 1
    assert len(sensor._on_remove) == 1
    sensor._on_remove[0]()
    assert len(runtime._listeners) == 0


@pytest.mark.asyncio
async def test_services_use_config_entry_runtime_data_and_validate_entries():
    hass = HomeAssistant()
    registered: dict[str, tuple] = {}

    hass.services.has_service = lambda domain, name: (domain, name) in registered

    def _register(domain, name, handler, schema=None):
        registered[(domain, name)] = (handler, schema)

    hass.services.async_register = _register

    assert await async_setup(hass, {}) is True
    assert (DOMAIN, SERVICE_RESYNC_SUBSCRIPTION) in registered
    assert (DOMAIN, SERVICE_SIMULATE_EVENT) in registered

    # No loaded entries -> ServiceValidationError
    hass.config_entries.async_loaded_entries = MagicMock(return_value=[])
    hass.config_entries.async_get_entry = MagicMock(return_value=None)

    sim_handler, sim_schema = registered[(DOMAIN, SERVICE_SIMULATE_EVENT)]
    resync_handler, _ = registered[(DOMAIN, SERVICE_RESYNC_SUBSCRIPTION)]

    with pytest.raises(ServiceValidationError):
        await sim_handler(SimpleNamespace(data=sim_schema({})))

    with pytest.raises(ServiceValidationError):
        await resync_handler(SimpleNamespace(data={"config_entry_id": "missing"}))

    # With a loaded config entry, runtime_data is used directly (no hass.data[DOMAIN])
    sub_manager = MagicMock()
    sub_manager.async_resync = AsyncMock(
        return_value={
            "subscription_id": 777,
            "callback_url": "https://ha.example.com/api/webhook/wh",
        }
    )
    entry = SimpleNamespace(
        entry_id="entry-1",
        domain=DOMAIN,
        state=ConfigEntryState.LOADED,
        options={},
    )
    runtime = StravaWebhookRuntime(
        entry=entry,
        session=MagicMock(),
        api=MagicMock(),
        subscription_manager=sub_manager,
        athlete_id=35638399,
        athlete_name="Jane Doe",
        device_id="dev-1",
        subscription_id=777,
    )
    entry.runtime_data = runtime

    hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)

    await resync_handler(SimpleNamespace(data={"config_entry_id": "entry-1"}))
    sub_manager.async_resync.assert_awaited_once()

    await sim_handler(SimpleNamespace(data=sim_schema({"sport_type": "Run"})))
    hass.bus.async_fire.assert_called_once()
    event_name, payload = hass.bus.async_fire.call_args.args
    assert event_name == EVENT_STRAVA_WEBHOOK
    assert payload["sport_type"] == "Run"
    assert payload["enriched"] is True
