"""Tests for Strava API client retries and token verification."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.strava_webhook.api import (
    StravaApiClient,
    StravaAuthError,
    StravaNotFoundError,
    async_verify_token_and_fetch_athlete,
)


class _FakeResponse:
    def __init__(self, status: int, json_data):
        self.status = status
        self._json = json_data

    async def json(self):
        return self._json


class _FakeClientSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        resp = self._responses.pop(0)

        @asynccontextmanager
        async def _ctx():
            yield resp

        return _ctx()


@pytest.mark.asyncio
async def test_verify_token_and_fetch_athlete_succeeds():
    from homeassistant.core import HomeAssistant

    hass = HomeAssistant()
    hass.data["_test_session"] = _FakeClientSession(
        [
            _FakeResponse(200, {"id": 42, "firstname": "Jan", "lastname": "Kowalski"}),
            _FakeResponse(200, []),
        ]
    )

    athlete = await async_verify_token_and_fetch_athlete(hass, "tok-123")
    assert athlete["id"] == 42


@pytest.mark.asyncio
async def test_verify_token_rejects_missing_activity_scope():
    from homeassistant.core import HomeAssistant

    hass = HomeAssistant()
    hass.data["_test_session"] = _FakeClientSession(
        [
            _FakeResponse(200, {"id": 42}),
            _FakeResponse(401, {"message": "Authorization Error"}),
        ]
    )

    with pytest.raises(StravaAuthError, match="missing_activity_scope"):
        await async_verify_token_and_fetch_athlete(hass, "tok-123")


@pytest.mark.asyncio
async def test_get_activity_retries_on_404_then_succeeds():
    from homeassistant.core import HomeAssistant

    hass = HomeAssistant()
    hass.data["_test_session"] = _FakeClientSession(
        [
            _FakeResponse(404, {"message": "Not Found"}),
            _FakeResponse(200, {"id": 777, "sport_type": "WeightTraining"}),
        ]
    )
    session = MagicMock()
    session.async_ensure_token_valid = AsyncMock()
    session.token = {"access_token": "tok-abc"}

    client = StravaApiClient(hass, session)
    activity = await client.async_get_activity(777, retries=2, retry_delays=(0.0,))
    assert activity["sport_type"] == "WeightTraining"


@pytest.mark.asyncio
async def test_get_activity_raises_404_after_retries_exhausted():
    from homeassistant.core import HomeAssistant

    hass = HomeAssistant()
    hass.data["_test_session"] = _FakeClientSession(
        [
            _FakeResponse(404, {}),
            _FakeResponse(404, {}),
        ]
    )
    session = MagicMock()
    session.async_ensure_token_valid = AsyncMock()
    session.token = {"access_token": "tok-abc"}

    client = StravaApiClient(hass, session)
    with pytest.raises(StravaNotFoundError):
        await client.async_get_activity(777, retries=1, retry_delays=(0.0,))
