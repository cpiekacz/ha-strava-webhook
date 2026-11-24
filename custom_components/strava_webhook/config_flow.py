"""Config flow and Options flow for Strava Webhook."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.config_entry_oauth2_flow import AbstractOAuth2FlowHandler

from .api import StravaApiError, StravaAuthError, async_verify_token_and_fetch_athlete
from .const import (
    CONF_ATHLETE_ID,
    CONF_ATHLETE_NAME,
    CONF_CLIENT_ID,
    CONF_CUSTOM_CALLBACK_URL,
    CONF_ENRICH_ACTIVITIES,
    CONF_REPLACE_EXISTING_SUBSCRIPTION,
    DEFAULT_ENRICH_ACTIVITIES,
    DEFAULT_REPLACE_EXISTING_SUBSCRIPTION,
    DOMAIN,
    OAUTH_SCOPES,
)
from .subscriptions import validate_callback_url
import voluptuous as vol

_LOGGER = logging.getLogger(__name__)


class StravaWebhookConfigFlow(AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Handle OAuth2 config flow for Strava Webhook."""

    DOMAIN = DOMAIN
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> StravaWebhookOptionsFlow:
        """Return the options flow for this config entry."""
        return StravaWebhookOptionsFlow()

    @property
    def logger(self) -> logging.Logger:
        """Return integration logger."""
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, str]:
        """Request minimal read-only Strava scopes."""
        return {
            "scope": OAUTH_SCOPES,
            "approval_prompt": "force",
        }

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> FlowResult:
        """Verify Strava athlete & activity scope and create or update the entry."""
        access_token = data["token"]["access_token"]
        try:
            athlete = await async_verify_token_and_fetch_athlete(self.hass, access_token)
        except StravaAuthError as err:
            if "missing_activity_scope" in str(err):
                return self.async_abort(reason="missing_activity_scope")
            return self.async_abort(reason="cannot_connect")
        except StravaApiError:
            return self.async_abort(reason="cannot_connect")

        athlete_id = str(athlete["id"])
        athlete_name = (
            f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip()
            or athlete.get("username")
            or f"Athlete {athlete_id}"
        )

        await self.async_set_unique_id(athlete_id)

        entry_data = {
            **data,
            CONF_ATHLETE_ID: athlete_id,
            CONF_ATHLETE_NAME: athlete_name,
            CONF_CLIENT_ID: str(self.flow_impl.client_id),
        }

        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            reauth_entry = self._get_reauth_entry()
            return self.async_update_reload_and_abort(
                reauth_entry, data_updates=entry_data
            )

        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Strava Webhook ({athlete_name})",
            data=entry_data,
            options={
                CONF_ENRICH_ACTIVITIES: DEFAULT_ENRICH_ACTIVITIES,
                CONF_CUSTOM_CALLBACK_URL: "",
                CONF_REPLACE_EXISTING_SUBSCRIPTION: DEFAULT_REPLACE_EXISTING_SUBSCRIPTION,
            },
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Prompt user before starting OAuth2 re-authorization."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm re-authorization using the existing auth implementation."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        reauth_entry = self._get_reauth_entry()
        return await self.async_step_pick_implementation(
            {"implementation": reauth_entry.data.get("auth_implementation")}
        )


class StravaWebhookOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Strava Webhook."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage Strava Webhook integration options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            custom_url = str(user_input.get(CONF_CUSTOM_CALLBACK_URL, "") or "").strip()
            if custom_url:
                try:
                    custom_url = validate_callback_url(custom_url)
                except ValueError:
                    errors[CONF_CUSTOM_CALLBACK_URL] = "invalid_callback_url"

            if not errors:
                return self.async_create_entry(
                    data={
                        CONF_ENRICH_ACTIVITIES: bool(
                            user_input.get(CONF_ENRICH_ACTIVITIES, DEFAULT_ENRICH_ACTIVITIES)
                        ),
                        CONF_CUSTOM_CALLBACK_URL: custom_url,
                        CONF_REPLACE_EXISTING_SUBSCRIPTION: bool(
                            user_input.get(
                                CONF_REPLACE_EXISTING_SUBSCRIPTION,
                                DEFAULT_REPLACE_EXISTING_SUBSCRIPTION,
                            )
                        ),
                    }
                )

        current_options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ENRICH_ACTIVITIES,
                    default=current_options.get(
                        CONF_ENRICH_ACTIVITIES, DEFAULT_ENRICH_ACTIVITIES
                    ),
                ): bool,
                vol.Optional(
                    CONF_CUSTOM_CALLBACK_URL,
                    default=current_options.get(CONF_CUSTOM_CALLBACK_URL, ""),
                ): str,
                vol.Required(
                    CONF_REPLACE_EXISTING_SUBSCRIPTION,
                    default=current_options.get(
                        CONF_REPLACE_EXISTING_SUBSCRIPTION,
                        DEFAULT_REPLACE_EXISTING_SUBSCRIPTION,
                    ),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
