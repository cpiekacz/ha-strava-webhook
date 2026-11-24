"""Application Credentials and OAuth2 implementation for Strava Webhook."""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import ClientError
from homeassistant.components.application_credentials import (
    AuthImplementation,
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    OAuth2TokenRequestConnectionError,
    OAuth2TokenRequestReauthError,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import AUTHORIZE_URL, DOMAIN, TOKEN_URL


def _validate_token_payload(value: Any) -> dict[str, Any]:
    """Validate Strava OAuth2 token response without leaking raw secrets."""
    if not isinstance(value, dict):
        raise OAuth2TokenRequestConnectionError(domain=DOMAIN)
    for key in ("access_token", "refresh_token"):
        token_val = value.get(key)
        if not isinstance(token_val, str) or not token_val.strip():
            raise OAuth2TokenRequestConnectionError(domain=DOMAIN)
    expires_in = value.get("expires_in")
    if type(expires_in) is not int or expires_in <= 0:
        raise OAuth2TokenRequestConnectionError(domain=DOMAIN)
    token_type = str(value.get("token_type", "Bearer"))
    if token_type.lower() != "bearer":
        raise OAuth2TokenRequestConnectionError(domain=DOMAIN)

    result: dict[str, Any] = {
        "access_token": value["access_token"],
        "refresh_token": value["refresh_token"],
        "expires_in": expires_in,
        "token_type": "Bearer",
    }
    if "expires_at" in value and isinstance(value["expires_at"], (int, float)):
        result["expires_at"] = value["expires_at"]
    if "scope" in value and isinstance(value["scope"], str):
        result["scope"] = value["scope"]
    return result


class StravaOAuthImplementation(AuthImplementation):
    """Strava OAuth2 implementation using HA Application Credentials."""

    async def _token_request(self, data: dict[str, Any]) -> dict[str, Any]:
        """Exchange or refresh OAuth2 tokens via form POST without logging secrets."""
        payload = {
            **data,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        try:
            async with asyncio.timeout(20):
                async with async_get_clientsession(self.hass).post(
                    self.token_url, data=payload, allow_redirects=False
                ) as response:
                    if response.status != 200:
                        if response.status in (400, 401, 403):
                            raise OAuth2TokenRequestReauthError(
                                domain=DOMAIN,
                                request_info=response.request_info,
                                status=response.status,
                            )
                        raise OAuth2TokenRequestConnectionError(domain=DOMAIN)
                    token_data = await response.json()
                    return _validate_token_payload(token_data)
        except (OAuth2TokenRequestConnectionError, OAuth2TokenRequestReauthError):
            raise
        except (ClientError, TimeoutError, ValueError):
            raise OAuth2TokenRequestConnectionError(domain=DOMAIN) from None


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Return Strava OAuth2 authorization server endpoints."""
    return AuthorizationServer(authorize_url=AUTHORIZE_URL, token_url=TOKEN_URL)


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> StravaOAuthImplementation:
    """Return the Strava OAuth2 implementation for Application Credentials."""
    return StravaOAuthImplementation(
        hass,
        auth_domain,
        credential,
        await async_get_authorization_server(hass),
    )
