"""The Strava Webhook integration."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.config_entry_oauth2_flow import (
    OAuth2Session,
    async_get_config_entry_implementation,
)
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.helpers.typing import ConfigType

from .api import StravaApiClient, StravaApiError, StravaAuthError
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
    PLATFORMS,
    redact_secrets,
)
from .event_builder import build_webhook_event_payload
from .runtime import (
    StravaWebhookConfigEntry,
    StravaWebhookRuntime,
    dispatch_runtime_event,
)
from .services import async_setup_services
from .subscriptions import (
    SubscriptionConflictError,
    SubscriptionError,
    SubscriptionManager,
    async_get_subscription_manager,
    parse_strava_id,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

__all__ = [
    "CONFIG_SCHEMA",
    "StravaWebhookConfigEntry",
    "StravaWebhookRuntime",
    "async_remove_entry",
    "async_setup",
    "async_setup_entry",
    "async_unload_entry",
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register domain-level services for Strava Webhook."""
    async_setup_services(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: StravaWebhookConfigEntry
) -> bool:
    """Set up Strava Webhook from a config entry."""
    try:
        implementation = await async_get_config_entry_implementation(hass, entry)
    except Exception as err:
        raise ConfigEntryNotReady(
            "Strava Application Credentials are not ready yet"
        ) from err

    session = OAuth2Session(hass, entry, implementation)
    try:
        await session.async_ensure_token_valid()
    except (ClientError, TimeoutError) as err:
        raise ConfigEntryNotReady("Strava token refresh temporarily unavailable") from err
    except Exception as err:
        raise ConfigEntryAuthFailed(
            "Strava authentication failed; reauthorization required"
        ) from err

    athlete_id = parse_strava_id(entry.data.get(CONF_ATHLETE_ID) or entry.unique_id)
    if athlete_id is None:
        raise ConfigEntryAuthFailed("Missing valid athlete_id in config entry")

    athlete_name = str(entry.data.get(CONF_ATHLETE_NAME) or f"Athlete {athlete_id}")
    client_id = str(entry.data.get(CONF_CLIENT_ID) or implementation.client_id)

    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, str(athlete_id))},
        manufacturer="Strava",
        model="Push Webhook",
        name=f"Strava ({athlete_name})",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://www.strava.com/settings/api",
    )

    api = StravaApiClient(hass, session)
    manager = await async_get_subscription_manager(
        hass, client_id, implementation.client_secret
    )

    runtime = StravaWebhookRuntime(
        entry=entry,
        session=session,
        api=api,
        subscription_manager=manager,
        athlete_id=athlete_id,
        athlete_name=athlete_name,
        device_id=device_entry.id,
    )

    async def _async_process_webhook(raw_webhook: dict[str, Any]) -> None:
        object_type = raw_webhook.get("object_type")
        aspect_type = raw_webhook.get("aspect_type")
        object_id = raw_webhook.get("object_id")
        updates = raw_webhook.get("updates")
        if not isinstance(updates, dict):
            updates = {}

        if (
            object_type == "athlete"
            and str(updates.get("authorized", "")).lower() == "false"
        ):
            _LOGGER.warning(
                "Received Strava deauthorization webhook for athlete_id=%s; starting reauth",
                athlete_id,
            )
            payload = build_webhook_event_payload(
                raw_webhook,
                config_entry_id=entry.entry_id,
                device_id=runtime.device_id,
                athlete_id=athlete_id,
                activity=None,
                enriched=False,
            )
            dispatch_runtime_event(hass, runtime, payload)
            entry.async_start_reauth(hass)
            return

        activity_data: dict[str, Any] | None = None
        enriched = False
        should_enrich = (
            object_type == "activity"
            and aspect_type in ("create", "update")
            and entry.options.get(CONF_ENRICH_ACTIVITIES, DEFAULT_ENRICH_ACTIVITIES)
        )

        if should_enrich and object_id is not None:
            try:
                activity_data = await api.async_get_activity(object_id)
                enriched = True
            except StravaAuthError:
                _LOGGER.warning(
                    "Strava authorization rejected while fetching activity %s; starting reauth",
                    object_id,
                )
                entry.async_start_reauth(hass)
            except StravaApiError as err:
                _LOGGER.warning(
                    "Could not enrich Strava activity %s (%s); emitting raw webhook payload",
                    object_id,
                    err,
                )

        payload = build_webhook_event_payload(
            raw_webhook,
            config_entry_id=entry.entry_id,
            device_id=runtime.device_id,
            athlete_id=athlete_id,
            activity=activity_data,
            enriched=enriched,
        )
        dispatch_runtime_event(hass, runtime, payload)

    async def _on_webhook_received(raw_webhook: dict[str, Any]) -> None:
        # Return HTTP 200 to Strava immediately; run API enrichment in background.
        _LOGGER.info(
            "Received Strava webhook: object_type=%s aspect_type=%s object_id=%s owner_id=%s",
            raw_webhook.get("object_type"),
            raw_webhook.get("aspect_type"),
            raw_webhook.get("object_id"),
            raw_webhook.get("owner_id"),
        )
        entry.async_create_background_task(
            hass,
            _async_process_webhook(raw_webhook),
            f"{DOMAIN}_process_{athlete_id}_{raw_webhook.get('object_id')}",
        )

    options = entry.options
    try:
        sub_info = await manager.async_connect(
            athlete_id,
            _on_webhook_received,
            custom_callback_url=options.get(CONF_CUSTOM_CALLBACK_URL),
            replace_existing=options.get(
                CONF_REPLACE_EXISTING_SUBSCRIPTION,
                DEFAULT_REPLACE_EXISTING_SUBSCRIPTION,
            ),
        )
    except NoURLAvailableError as err:
        raise ConfigEntryNotReady(
            "Configure a public HTTPS External URL or custom callback URL for Strava webhooks"
        ) from err
    except SubscriptionConflictError as err:
        raise ConfigEntryNotReady(
            f"Strava webhook conflict: {err.existing_url} already subscribed. "
            "Enable 'Replace existing subscription' in integration options if you want to take it over."
        ) from err
    except SubscriptionError as err:
        raise ConfigEntryNotReady(
            f"Failed to register Strava webhook subscription: {err}"
        ) from err

    runtime.subscription_id = sub_info["subscription_id"]
    runtime.callback_url = sub_info["callback_url"]
    runtime.status = "connected"

    entry.runtime_data = runtime

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: StravaWebhookConfigEntry
) -> bool:
    """Unload Strava Webhook entry without deleting remote Strava subscription."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    runtime: StravaWebhookRuntime | None = getattr(entry, "runtime_data", None)
    if runtime is not None:
        await runtime.subscription_manager.async_disconnect(runtime.athlete_id)

    return unload_ok


async def async_remove_entry(
    hass: HomeAssistant, entry: StravaWebhookConfigEntry
) -> None:
    """Delete the Strava push subscription when the integration entry is removed."""
    client_id = entry.data.get(CONF_CLIENT_ID)
    if not client_id:
        try:
            impl = await async_get_config_entry_implementation(hass, entry)
            client_id = str(impl.client_id)
        except (HomeAssistantError, ValueError, RuntimeError):
            client_id = None

    if not client_id:
        _LOGGER.debug(
            "Skipping Strava subscription cleanup for entry %s: no client_id available",
            entry.entry_id,
        )
        return

    client_key = str(client_id)
    other_entries = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id
        and str(e.data.get(CONF_CLIENT_ID, "")) == client_key
    ]
    if other_entries:
        _LOGGER.debug(
            "Keeping Strava push subscription for client_id=%s (still used by %d other entry)",
            client_key,
            len(other_entries),
        )
        return

    managers: dict[str, SubscriptionManager] = hass.data.get(
        f"{DOMAIN}_subscriptions", {}
    )
    manager = managers.get(client_key)

    if manager is None:
        try:
            impl = await async_get_config_entry_implementation(hass, entry)
            manager = await async_get_subscription_manager(
                hass, client_key, impl.client_secret
            )
        except (HomeAssistantError, ValueError, RuntimeError) as err:
            _LOGGER.warning(
                "Could not load Strava credentials to delete push subscription on entry removal: %s",
                redact_secrets(str(err)),
            )
            return

    try:
        await manager.async_delete_remote_subscription()
    except (SubscriptionError, ClientError, TimeoutError, OSError) as err:
        _LOGGER.warning(
            "Failed to delete Strava push subscription for client_id=%s during removal: %s",
            client_key,
            redact_secrets(str(err)),
        )
    finally:
        managers.pop(client_key, None)
