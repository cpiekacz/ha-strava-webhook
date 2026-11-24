"""The Strava Webhook integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
import time
from typing import Any

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ServiceValidationError,
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
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .api import StravaApiClient, StravaApiError, StravaAuthError
from .const import (
    ASPECT_TYPES,
    CONF_ATHLETE_ID,
    CONF_ATHLETE_NAME,
    CONF_CLIENT_ID,
    CONF_CUSTOM_CALLBACK_URL,
    CONF_ENRICH_ACTIVITIES,
    CONF_REPLACE_EXISTING_SUBSCRIPTION,
    DEFAULT_ENRICH_ACTIVITIES,
    DEFAULT_REPLACE_EXISTING_SUBSCRIPTION,
    DOMAIN,
    EVENT_STRAVA_WEBHOOK,
    OBJECT_TYPES,
    PLATFORMS,
    SERVICE_RESYNC_SUBSCRIPTION,
    SERVICE_SIMULATE_EVENT,
    redact_secrets,
)
from .event_builder import build_webhook_event_payload
from .subscriptions import (
    SubscriptionConflictError,
    SubscriptionError,
    SubscriptionManager,
    async_get_subscription_manager,
    parse_strava_id,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SIMULATE_EVENT_SCHEMA = vol.Schema(
    {
        vol.Optional("config_entry_id"): cv.string,
        vol.Optional("object_type", default="activity"): vol.In(OBJECT_TYPES),
        vol.Optional("aspect_type", default="create"): vol.In(ASPECT_TYPES),
        vol.Optional("object_id", default=999999999): vol.Coerce(int),
        vol.Optional("sport_type", default="WeightTraining"): cv.string,
        vol.Optional("name", default="Strength Training (test)"): cv.string,
        vol.Optional("updates", default=dict): dict,
        vol.Optional("extra_activity_data", default=dict): dict,
    }
)

RESYNC_SUBSCRIPTION_SCHEMA = vol.Schema(
    {
        vol.Optional("config_entry_id"): cv.string,
    }
)


@dataclass
class StravaWebhookRuntime:
    """Runtime state for a configured Strava Webhook entry."""

    entry: ConfigEntry
    session: OAuth2Session
    api: StravaApiClient
    subscription_manager: SubscriptionManager
    athlete_id: int
    athlete_name: str
    device_id: str
    subscription_id: int | None = None
    callback_url: str | None = None
    status: str = "connected"
    events_received: int = 0
    last_event_time: str | None = None
    last_payload: dict[str, Any] | None = None
    _listeners: list[Callable[[dict[str, Any]], None]] = field(default_factory=list)

    @callback
    def add_listener(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Register an entity listener for incoming webhook events."""
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def notify_listeners(self, payload: dict[str, Any]) -> None:
        """Notify all attached entities of a new event or status update."""
        for listener in list(self._listeners):
            listener(payload)


def _get_target_runtimes(
    hass: HomeAssistant, config_entry_id: str | None = None
) -> list[StravaWebhookRuntime]:
    """Resolve one or all loaded StravaWebhookRuntime instances."""
    runtimes: dict[str, StravaWebhookRuntime] = hass.data.get(DOMAIN, {})
    if config_entry_id:
        runtime = runtimes.get(config_entry_id)
        if runtime is None:
            raise ServiceValidationError(
                f"Strava Webhook config entry '{config_entry_id}' is not loaded"
            )
        return [runtime]
    if not runtimes:
        raise ServiceValidationError("No loaded Strava Webhook integration found")
    return list(runtimes.values())


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register domain-level services for Strava Webhook."""

    async def _handle_resync_subscription(call: ServiceCall) -> None:
        runtimes = _get_target_runtimes(hass, call.data.get("config_entry_id"))
        for runtime in runtimes:
            options = runtime.entry.options
            result = await runtime.subscription_manager.async_resync(
                custom_callback_url=options.get(CONF_CUSTOM_CALLBACK_URL),
                replace_existing=options.get(
                    CONF_REPLACE_EXISTING_SUBSCRIPTION,
                    DEFAULT_REPLACE_EXISTING_SUBSCRIPTION,
                ),
            )
            runtime.subscription_id = result["subscription_id"]
            runtime.callback_url = result["callback_url"]
            runtime.status = "connected"
            runtime.notify_listeners(runtime.last_payload or {})

    async def _handle_simulate_event(call: ServiceCall) -> None:
        runtimes = _get_target_runtimes(hass, call.data.get("config_entry_id"))
        runtime = runtimes[0]

        object_type = call.data["object_type"]
        aspect_type = call.data["aspect_type"]
        object_id = call.data["object_id"]
        sport_type = call.data["sport_type"]
        name = call.data["name"]
        updates = dict(call.data.get("updates") or {})
        extra_activity = dict(call.data.get("extra_activity_data") or {})

        raw_webhook = {
            "aspect_type": aspect_type,
            "event_time": int(time.time()),
            "object_id": object_id,
            "object_type": object_type,
            "owner_id": runtime.athlete_id,
            "subscription_id": runtime.subscription_id or 0,
            "updates": updates,
        }

        activity_data: dict[str, Any] | None = None
        enriched = False
        if object_type == "activity" and aspect_type in ("create", "update"):
            enriched = True
            activity_data = {
                "id": object_id,
                "name": name,
                "sport_type": sport_type,
                "type": sport_type,
                "distance": 0.0,
                "moving_time": 3600,
                "elapsed_time": 3700,
                "total_elevation_gain": 0.0,
                "start_date_local": dt_util.now().isoformat(),
                "trainer": False,
                "commute": False,
                "manual": False,
                "private": False,
                "device_name": "Simulated Device",
                **extra_activity,
            }

        payload = build_webhook_event_payload(
            raw_webhook,
            config_entry_id=runtime.entry.entry_id,
            device_id=runtime.device_id,
            athlete_id=runtime.athlete_id,
            activity=activity_data,
            enriched=enriched,
        )
        _dispatch_runtime_event(hass, runtime, payload)

    if not hass.services.has_service(DOMAIN, SERVICE_RESYNC_SUBSCRIPTION):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RESYNC_SUBSCRIPTION,
            _handle_resync_subscription,
            schema=RESYNC_SUBSCRIPTION_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SIMULATE_EVENT):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SIMULATE_EVENT,
            _handle_simulate_event,
            schema=SIMULATE_EVENT_SCHEMA,
        )

    return True


def _dispatch_runtime_event(
    hass: HomeAssistant, runtime: StravaWebhookRuntime, payload: dict[str, Any]
) -> None:
    """Update runtime statistics, notify entities, and fire the HA bus event."""
    runtime.events_received += 1
    runtime.last_event_time = dt_util.utcnow().isoformat()
    runtime.last_payload = payload
    runtime.notify_listeners(payload)
    hass.bus.async_fire(EVENT_STRAVA_WEBHOOK, payload)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
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
        raise ConfigEntryAuthFailed("Strava authentication failed; reauthorization required") from err

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

        if object_type == "athlete" and str(updates.get("authorized", "")).lower() == "false":
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
            _dispatch_runtime_event(hass, runtime, payload)
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
        _dispatch_runtime_event(hass, runtime, payload)

    async def _on_webhook_received(raw_webhook: dict[str, Any]) -> None:
        # Return HTTP 200 to Strava immediately; run API enrichment in background.
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

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    entry.runtime_data = runtime

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload Strava Webhook entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Strava Webhook entry without deleting remote Strava subscription."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    runtime: StravaWebhookRuntime | None = hass.data.get(DOMAIN, {}).pop(
        entry.entry_id, None
    )
    if runtime is not None:
        await runtime.subscription_manager.async_disconnect(runtime.athlete_id)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the Strava push subscription when the integration entry is removed."""
    client_id = entry.data.get(CONF_CLIENT_ID)
    if not client_id:
        try:
            impl = await async_get_config_entry_implementation(hass, entry)
            client_id = str(impl.client_id)
        except Exception:
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
        if e.entry_id != entry.entry_id and str(e.data.get(CONF_CLIENT_ID, "")) == client_key
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
        except Exception as err:
            _LOGGER.warning(
                "Could not load Strava credentials to delete push subscription on entry removal: %s",
                redact_secrets(str(err)),
            )
            return

    try:
        await manager.async_delete_remote_subscription()
    except Exception as err:
        _LOGGER.warning(
            "Failed to delete Strava push subscription for client_id=%s during removal: %s",
            client_key,
            redact_secrets(str(err)),
        )
    finally:
        managers.pop(client_key, None)
