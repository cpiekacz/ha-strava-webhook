"""Tests for WebhookReceiver and SubscriptionManager (including delete on removal)."""

from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.strava_webhook.subscriptions import (
    SubscriptionConflictError,
    SubscriptionManager,
    WebhookReceiver,
    parse_strava_id,
    validate_callback_url,
)


class _FakeStream:
    def __init__(self, data: bytes):
        self._data = data
        self._offset = 0

    async def read(self, n: int) -> bytes:
        chunk = self._data[self._offset : self._offset + n]
        self._offset += len(chunk)
        return chunk


class _FakeRequest:
    def __init__(self, method: str, *, query=None, body: bytes = b""):
        self.method = method
        self.query = query or {}
        self.content = _FakeStream(body)


class _FakeStore:
    def __init__(self, initial=None):
        self.data = initial
        self.removed = False

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = dict(data)
        self.removed = False

    async def async_remove(self):
        self.data = None
        self.removed = True


class _FakeApiResponse:
    def __init__(self, status: int, json_data=None, text_data: str = ""):
        self.status = status
        self._json = json_data
        self._text = text_data

    async def json(self):
        return self._json

    async def text(self):
        return self._text


class _FakeHttpSession:
    def __init__(self, handler):
        self._handler = handler
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        resp = self._handler(method, url, kwargs)

        @asynccontextmanager
        async def _ctx():
            yield resp

        return _ctx()


def test_parse_strava_id_and_url_validation():
    assert parse_strava_id(123) == 123
    assert parse_strava_id("456") == 456
    assert parse_strava_id(True) is None
    assert parse_strava_id(0) is None
    assert parse_strava_id(-5) is None
    assert parse_strava_id("012") is None

    assert validate_callback_url("https://ha.example.com/api/webhook/abc") == "https://ha.example.com/api/webhook/abc"
    with pytest.raises(ValueError):
        validate_callback_url("http://ha.example.com/api/webhook/abc")
    with pytest.raises(ValueError):
        validate_callback_url("https://user:pass@ha.example.com/api/webhook/abc")


@pytest.mark.asyncio
async def test_webhook_receiver_get_challenge_and_post_events():
    received = []

    async def _target(payload):
        received.append(payload)

    receiver = WebhookReceiver(
        subscription_id=1001,
        verify_token="secret-token",
        targets={555: _target},
    )

    # Valid GET challenge
    resp = await receiver.handle(
        None,
        "wh",
        _FakeRequest(
            "GET",
            query={
                "hub.mode": "subscribe",
                "hub.verify_token": "secret-token",
                "hub.challenge": "abc123",
            },
        ),
    )
    assert resp.status == 200
    assert json.loads(resp.text) == {"hub.challenge": "abc123"}

    # Invalid verify_token
    bad_resp = await receiver.handle(
        None,
        "wh",
        _FakeRequest(
            "GET",
            query={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong",
                "hub.challenge": "abc123",
            },
        ),
    )
    assert bad_resp.status == 400

    # Valid POST update event
    payload = {
        "aspect_type": "update",
        "event_time": 1732474000,
        "object_id": 999,
        "object_type": "activity",
        "owner_id": 555,
        "subscription_id": 1001,
        "updates": {"title": "New Title"},
    }
    post_resp = await receiver.handle(
        None, "wh", _FakeRequest("POST", body=json.dumps(payload).encode("utf-8"))
    )
    assert post_resp.status == 200
    assert received == [payload]

    # Oversized POST -> 413
    big_resp = await receiver.handle(None, "wh", _FakeRequest("POST", body=b"x" * 20000))
    assert big_resp.status == 413


@pytest.mark.asyncio
async def test_subscription_manager_connect_conflict_replace_and_delete():
    from homeassistant.core import HomeAssistant

    hass = HomeAssistant()
    remote_subs = []

    def _http_handler(method, url, kwargs):
        if method == "GET":
            return _FakeApiResponse(200, list(remote_subs))
        if method == "POST":
            data = kwargs["data"]
            created = {"id": 7777, "callback_url": data["callback_url"]}
            remote_subs.append(created)
            return _FakeApiResponse(201, {"id": 7777})
        if method == "DELETE":
            sub_id = int(url.rsplit("/", 1)[-1])
            remote_subs[:] = [s for s in remote_subs if s["id"] != sub_id]
            return _FakeApiResponse(204, None)
        raise AssertionError(f"Unexpected {method}")

    http_session = _FakeHttpSession(_http_handler)
    hass.data["_test_session"] = http_session

    store = _FakeStore()
    manager = SubscriptionManager(hass, "12345", "super-secret", store)

    # 1. Connect creates remote subscription
    info = await manager.async_connect(555, AsyncMock())
    assert info["subscription_id"] == 7777
    assert len(remote_subs) == 1
    assert store.data["subscription_id"] == "7777"

    # 2. Delete remote subscription on integration removal
    deleted = await manager.async_delete_remote_subscription()
    assert deleted is True
    assert remote_subs == []
    assert store.removed is True

    # 3. Conflict detection vs replace_existing=True
    remote_subs.append({"id": 8888, "callback_url": "https://other.example.com/hook"})
    manager2 = SubscriptionManager(hass, "12345", "super-secret", _FakeStore())
    with pytest.raises(SubscriptionConflictError):
        await manager2.async_connect(555, AsyncMock(), replace_existing=False)

    info2 = await manager2.async_connect(555, AsyncMock(), replace_existing=True)
    assert info2["subscription_id"] == 7777
    assert len(remote_subs) == 1
    assert remote_subs[0]["id"] == 7777
