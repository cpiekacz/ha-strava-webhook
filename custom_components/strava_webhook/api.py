"""Strava REST API client for athlete verification and activity enrichment."""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

from .const import ACTIVITIES_URL, ATHLETE_ACTIVITIES_URL, ATHLETE_URL, redact_secrets


class StravaApiError(Exception):
    """Base error raised by Strava REST API calls."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(redact_secrets(message))
        self.status = status


class StravaAuthError(StravaApiError):
    """Raised on 401/403 or missing activity scope."""


class StravaNotFoundError(StravaApiError):
    """Raised on HTTP 404."""


class StravaRateLimitError(StravaApiError):
    """Raised on HTTP 429."""


async def _fetch_json_with_token(
    hass: HomeAssistant,
    url: str,
    access_token: str,
    *,
    params: dict[str, Any] | None = None,
) -> Any:
    """Perform a GET request against Strava API using a Bearer token."""
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with asyncio.timeout(20):
            async with async_get_clientsession(hass).get(
                url,
                headers=headers,
                params=params,
                allow_redirects=False,
            ) as response:
                if response.status in (401, 403):
                    raise StravaAuthError(
                        f"Strava authorization failed ({response.status})",
                        status=response.status,
                    )
                if response.status == 404:
                    raise StravaNotFoundError(
                        "Strava resource not found (404)",
                        status=404,
                    )
                if response.status == 429:
                    raise StravaRateLimitError(
                        "Strava API rate limit exceeded (429)",
                        status=429,
                    )
                if response.status >= 400:
                    raise StravaApiError(
                        f"Strava API returned HTTP {response.status}",
                        status=response.status,
                    )
                return await response.json()
    except StravaApiError:
        raise
    except (ClientError, TimeoutError, ValueError) as err:
        raise StravaApiError(f"Strava API connection error: {type(err).__name__}") from None


async def async_verify_token_and_fetch_athlete(
    hass: HomeAssistant, access_token: str
) -> dict[str, Any]:
    """Verify OAuth token, confirm activity read access, and return athlete profile.

    Strava does not reliably return the granted `scope` field in /oauth/token JSON,
    so activity read permission is explicitly verified against /athlete/activities.
    """
    athlete = await _fetch_json_with_token(hass, ATHLETE_URL, access_token)
    if not isinstance(athlete, dict) or not athlete.get("id"):
        raise StravaApiError("Invalid athlete profile response from Strava")

    try:
        activities = await _fetch_json_with_token(
            hass, ATHLETE_ACTIVITIES_URL, access_token, params={"per_page": 1}
        )
    except StravaAuthError as err:
        raise StravaAuthError("missing_activity_scope", status=err.status) from None

    if not isinstance(activities, list):
        raise StravaApiError("Invalid activities probe response from Strava")

    return athlete


class StravaApiClient:
    """Authenticated Strava API client backed by HA OAuth2Session."""

    def __init__(self, hass: HomeAssistant, session: OAuth2Session) -> None:
        self.hass = hass
        self.session = session

    async def _async_get(
        self, url: str, *, params: dict[str, Any] | None = None
    ) -> Any:
        await self.session.async_ensure_token_valid()
        access_token = self.session.token["access_token"]
        return await _fetch_json_with_token(
            self.hass, url, access_token, params=params
        )

    async def async_get_athlete(self) -> dict[str, Any]:
        """Fetch authenticated athlete profile."""
        data = await self._async_get(ATHLETE_URL)
        if not isinstance(data, dict) or not data.get("id"):
            raise StravaApiError("Invalid athlete response from Strava")
        return data

    async def async_get_activity(
        self,
        activity_id: int | str,
        *,
        retries: int = 3,
        retry_delays: tuple[float, ...] = (1.0, 2.0, 4.0),
    ) -> dict[str, Any]:
        """Fetch detailed Strava activity with bounded retries for eventual consistency."""
        url = f"{ACTIVITIES_URL}/{activity_id}"
        max_attempts = max(1, 1 + retries)

        for attempt in range(max_attempts):
            try:
                data = await self._async_get(url, params={"include_all_efforts": "false"})
                if not isinstance(data, dict):
                    raise StravaApiError("Invalid activity response from Strava")
                return data
            except (StravaAuthError, StravaRateLimitError):
                raise
            except StravaNotFoundError:
                if attempt >= max_attempts - 1:
                    raise
            except StravaApiError as err:
                # Retry transient 5xx or connection errors; fail fast on 4xx.
                if err.status is not None and 400 <= err.status < 500:
                    raise
                if attempt >= max_attempts - 1:
                    raise

            delay = retry_delays[min(attempt, len(retry_delays) - 1)]
            await asyncio.sleep(delay)

        raise StravaApiError("Failed to fetch activity from Strava")
