"""Tests for integration setup, unload, and remote subscription removal on entry deletion."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.strava_webhook import async_remove_entry, async_unload_entry
from custom_components.strava_webhook.const import CONF_CLIENT_ID, DOMAIN


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
