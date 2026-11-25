"""Tests for integration setup, unload, and remote subscription removal on entry deletion."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.strava_webhook import (
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.strava_webhook.config_flow import StravaWebhookOptionsFlow
from custom_components.strava_webhook.const import (
    CONF_ATHLETE_ID,
    CONF_ATHLETE_NAME,
    CONF_CLIENT_ID,
    CONF_ENRICH_ACTIVITIES,
    DOMAIN,
    EVENT_STRAVA_WEBHOOK,
)


class _FakeResponse:
    def __init__(self, status: int, json_data=None):
        self.status = status
        self._json = json_data

    async def json(self):
        return self._json

    async def text(self):
        return ""


class _FakeSession:
    def __init__(self, subs):
        self.subs = subs
        self.deleted_ids = []

    def request(self, method, url, **kwargs):
        if method == "GET":
            resp = _FakeResponse(200, list(self.subs))
        elif method == "DELETE":
            sub_id = int(url.rsplit("/", 1)[-1])
            self.deleted_ids.append(sub_id)
            self.subs[:] = [s for s in self.subs if s["id"] != sub_id]
            resp = _FakeResponse(204)
        else:
            raise AssertionError(f"Unexpected {method}")

        @asynccontextmanager
        async def _ctx():
            yield resp

        return _ctx()


@pytest.mark.asyncio
async def test_remove_entry_deletes_remote_subscription_even_after_unload():
    from homeassistant.core import HomeAssistant
    from custom_components.strava_webhook.subscriptions import SubscriptionManager

    hass = HomeAssistant()
    remote_subs = [{"id": 9001, "callback_url": "https://ha.example.com/api/webhook/wh_1"}]
    fake_http = _FakeSession(remote_subs)
    hass.data["_test_session"] = fake_http

    entry = SimpleNamespace(
        entry_id="entry-1",
        data={CONF_CLIENT_ID: "client-1"},
        options={},
    )
    hass.config_entries.async_entries = MagicMock(return_value=[])
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    # Simulate manager previously created during setup with persisted record
    store = MagicMock()
    store.async_load = AsyncMock(
        return_value={
            "webhook_id": "wh_1",
            "verify_token": "tok",
            "subscription_id": "9001",
            "callback_url": "https://ha.example.com/api/webhook/wh_1",
        }
    )
    store.async_remove = AsyncMock()
    manager = SubscriptionManager(hass, "client-1", "secret-1", store)
    hass.data[f"{DOMAIN}_subscriptions"] = {"client-1": manager}

    # Unload must NOT delete remote subscription
    runtime = SimpleNamespace(athlete_id=555, subscription_manager=manager)
    hass.data[DOMAIN] = {"entry-1": runtime}
    assert await async_unload_entry(hass, entry) is True
    assert fake_http.deleted_ids == []

    # Remove entry MUST delete remote subscription
    await async_remove_entry(hass, entry)
    assert fake_http.deleted_ids == [9001]
    assert remote_subs == []
    store.async_remove.assert_awaited_once()
    assert "client-1" not in hass.data[f"{DOMAIN}_subscriptions"]


@pytest.mark.asyncio
async def test_remove_entry_skips_delete_when_other_entry_shares_client_id():
    from homeassistant.core import HomeAssistant

    hass = HomeAssistant()
    remote_subs = [{"id": 9001, "callback_url": "https://ha.example.com/api/webhook/wh_1"}]
    fake_http = _FakeSession(remote_subs)
    hass.data["_test_session"] = fake_http

    entry1 = SimpleNamespace(entry_id="entry-1", data={CONF_CLIENT_ID: "client-1"})
    entry2 = SimpleNamespace(entry_id="entry-2", data={CONF_CLIENT_ID: "client-1"})
    hass.config_entries.async_entries = MagicMock(return_value=[entry1, entry2])

    await async_remove_entry(hass, entry1)
    assert fake_http.deleted_ids == []
    assert len(remote_subs) == 1


def test_options_flow_uses_options_flow_with_reload():
    from homeassistant import config_entries

    assert issubclass(StravaWebhookOptionsFlow, config_entries.OptionsFlowWithReload)
    assert StravaWebhookOptionsFlow.automatic_reload is True


@pytest.mark.asyncio
async def test_token_refresh_during_webhook_does_not_reload_or_cancel_event():
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import device_registry as dr

    hass = HomeAssistant()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_reload = AsyncMock()
    hass.config_entries.async_update_entry = MagicMock()

    fake_device_registry = MagicMock()
    fake_device_registry.async_get_or_create.return_value = SimpleNamespace(id="dev-1")
    dr.async_get.return_value = fake_device_registry

    background_tasks: list[asyncio.Task] = []

    def _create_bg_task(_hass, coro, _name):
        task = asyncio.create_task(coro)
        background_tasks.append(task)
        return task

    entry = SimpleNamespace(
        entry_id="entry-1",
        unique_id="35638399",
        data={
            CONF_ATHLETE_ID: "35638399",
            CONF_ATHLETE_NAME: "Test Athlete",
            CONF_CLIENT_ID: "32206",
            "token": {"access_token": "old-token", "expires_at": 1},
        },
        options={CONF_ENRICH_ACTIVITIES: True},
        add_update_listener=MagicMock(),
        async_on_unload=MagicMock(),
        async_create_background_task=_create_bg_task,
        async_start_reauth=MagicMock(),
    )

    impl = SimpleNamespace(client_id="32206", client_secret="secret-1")

    captured_handler = {}

    class _FakeManager:
        async def async_connect(self, athlete_id, handler, **kwargs):
            captured_handler["fn"] = handler
            return {
                "subscription_id": 390670,
                "callback_url": "https://ha.example.com/api/webhook/wh_1",
                "webhook_id": "wh_1",
            }

    class _RefreshingSession:
        def __init__(self, _hass, _entry, _impl):
            self.token = {"access_token": "fresh-token"}
            self.calls = 0

        async def async_ensure_token_valid(self):
            self.calls += 1
            # Simulate OAuth2Session saving a refreshed token to entry.data on webhook call.
            if self.calls >= 2:
                hass.config_entries.async_update_entry(
                    entry, data={**entry.data, "token": {"access_token": "refreshed-token"}}
                )
                self.token = {"access_token": "refreshed-token"}

    with (
        patch(
            "custom_components.strava_webhook.async_get_config_entry_implementation",
            AsyncMock(return_value=impl),
        ),
        patch(
            "custom_components.strava_webhook.OAuth2Session",
            _RefreshingSession,
        ),
        patch(
            "custom_components.strava_webhook.async_get_subscription_manager",
            AsyncMock(return_value=_FakeManager()),
        ),
        patch(
            "custom_components.strava_webhook.api._fetch_json_with_token",
            AsyncMock(
                return_value={
                    "id": 21122984921,
                    "name": "Evening Strength",
                    "sport_type": "WeightTraining",
                    "type": "WeightTraining",
                }
            ),
        ),
    ):
        assert await async_setup_entry(hass, entry) is True
        # No generic update listener may be registered; OptionsFlowWithReload handles option reloads.
        entry.add_update_listener.assert_not_called()

        await captured_handler["fn"](
            {
                "aspect_type": "create",
                "event_time": 1795631774,
                "object_id": 21122984921,
                "object_type": "activity",
                "owner_id": 35638399,
                "subscription_id": 390670,
                "updates": {},
            }
        )
        assert len(background_tasks) == 1
        await background_tasks[0]

    hass.config_entries.async_update_entry.assert_called_once()
    hass.config_entries.async_reload.assert_not_called()
    hass.bus.async_fire.assert_called_once()
    event_name, payload = hass.bus.async_fire.call_args.args
    assert event_name == EVENT_STRAVA_WEBHOOK
    assert payload["sport_type"] == "WeightTraining"
    assert payload["enriched"] is True
    assert payload["object_id"] == 21122984921
