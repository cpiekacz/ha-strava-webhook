"""Strava Push Subscription and HA Webhook management."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import ClientError, web
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from yarl import URL

from .const import DOMAIN, SUBSCRIPTIONS_URL, redact_secrets

_LOGGER = logging.getLogger(__name__)

MAX_WEBHOOK_BODY_BYTES = 16384


class SubscriptionError(Exception):
    """Base error for Strava push subscription management."""


class SubscriptionConflictError(SubscriptionError):
    """Raised when the Strava API app already has a subscription for another URL."""

    def __init__(self, existing_url: str, expected_url: str) -> None:
        super().__init__(
            f"Strava app already has a webhook subscription for {existing_url} "
            f"(expected {expected_url})"
        )
        self.existing_url = existing_url
        self.expected_url = expected_url


def parse_strava_id(value: Any) -> int | None:
    """Parse a positive Strava integer ID, rejecting booleans, floats, and negatives."""
    if type(value) is int:
        return value if 0 < value < 2**63 else None
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value):
        parsed = int(value)
        return parsed if parsed < 2**63 else None
    return None


def validate_callback_url(url: str) -> str:
    """Validate that a webhook callback URL is a clean public HTTPS URL."""
    if not isinstance(url, str):
        raise TypeError("Callback URL must be a string")
    cleaned = url.strip()
    if not cleaned or len(cleaned) > 255:
        raise ValueError("Callback URL length is invalid")
    parsed = URL(cleaned)
    if (
        parsed.scheme != "https"
        or not parsed.host
        or parsed.user
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("Callback URL must be an HTTPS URL without credentials or fragments")
    return cleaned


def _normalize_url(url: str | None) -> str:
    """Normalize callback URL for comparison."""
    if not url or not isinstance(url, str):
        return ""
    parsed = URL(url.strip())
    path = (parsed.path or "/").rstrip("/") or "/"
    port_part = f":{parsed.port}" if parsed.explicit_port else ""
    return f"{parsed.scheme.lower()}://{(parsed.host or '').lower()}{port_part}{path}"


class WebhookReceiver:
    """Handles Strava webhook GET verification and POST event delivery."""

    def __init__(
        self,
        *,
        subscription_id: int | None,
        verify_token: str,
        targets: dict[int, Callable[[dict[str, Any]], Awaitable[None]]],
        now: Callable[[], float] | None = None,
    ) -> None:
        self.subscription_id = subscription_id
        self.verify_token = verify_token
        self.targets = targets
        self.now = now or (lambda: dt_util.utcnow().timestamp())

    async def handle(
        self, hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        """Handle GET subscription validation and POST webhook events."""
        if request.method == "GET":
            query = request.query
            mode = query.get("hub.mode", "")
            token = query.get("hub.verify_token", "")
            challenge = query.get("hub.challenge", "")
            if (
                mode != "subscribe"
                or not secrets.compare_digest(token.encode("utf-8"), self.verify_token.encode("utf-8"))
                or not challenge
                or len(challenge) > 256
            ):
                _LOGGER.warning("Rejected Strava webhook GET challenge validation")
                return web.json_response({}, status=400)
            return web.json_response({"hub.challenge": challenge})

        if request.method != "POST":
            return web.json_response({}, status=405)

        stream = request.content
        raw = bytearray()
        while chunk := await stream.read(min(4096, MAX_WEBHOOK_BODY_BYTES + 1 - len(raw))):
            raw.extend(chunk)
            if len(raw) > MAX_WEBHOOK_BODY_BYTES:
                _LOGGER.warning("Rejected oversized Strava webhook payload (>%s bytes)", MAX_WEBHOOK_BODY_BYTES)
                return web.json_response({}, status=413)

        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            _LOGGER.warning("Rejected invalid JSON in Strava webhook POST")
            return web.json_response({}, status=400)

        if not isinstance(body, dict):
            return web.json_response({}, status=400)

        subscription_id = parse_strava_id(body.get("subscription_id"))
        owner_id = parse_strava_id(body.get("owner_id"))
        object_id = parse_strava_id(body.get("object_id"))
        event_time = parse_strava_id(body.get("event_time"))
        object_type = body.get("object_type")
        aspect_type = body.get("aspect_type")

        if (
            subscription_id is None
            or owner_id is None
            or object_id is None
            or event_time is None
            or not isinstance(object_type, str)
            or not object_type
            or not isinstance(aspect_type, str)
            or not aspect_type
        ):
            _LOGGER.warning("Rejected malformed Strava webhook payload: missing/invalid core fields")
            return web.json_response({}, status=400)

        if self.subscription_id is not None and subscription_id != self.subscription_id:
            _LOGGER.warning(
                "Ignored Strava webhook for mismatched subscription_id=%s (expected %s)",
                subscription_id,
                self.subscription_id,
            )
            return web.json_response({})

        target = self.targets.get(owner_id)
        if target is None:
            _LOGGER.debug("Ignored Strava webhook for unconfigured owner_id=%s", owner_id)
            return web.json_response({})

        # Target must schedule work asynchronously and return immediately (< 2s Strava limit).
        await target(body)
        return web.json_response({})


class SubscriptionManager:
    """Manages one Strava Push Subscription per API application (client_id)."""

    def __init__(
        self,
        hass: HomeAssistant,
        client_id: str,
        client_secret: str,
        store: Store[dict[str, Any]],
    ) -> None:
        self.hass = hass
        self.client_id = str(client_id)
        self.client_secret = client_secret
        self.store = store
        self.targets: dict[int, Callable[[dict[str, Any]], Awaitable[None]]] = {}
        self.record: dict[str, Any] = {}
        self.receiver: WebhookReceiver | None = None
        self.registered = False
        self.lock = asyncio.Lock()

    async def _ensure_loaded(self, *, create_if_missing: bool = True) -> dict[str, Any] | None:
        """Load persisted webhook/subscription state from Store."""
        if self.record:
            return self.record

        loaded = await self.store.async_load()
        if loaded is None:
            if not create_if_missing:
                return None
            loaded = {
                "webhook_id": webhook.async_generate_id(),
                "verify_token": secrets.token_urlsafe(32),
                "subscription_id": None,
                "callback_url": None,
            }
            await self.store.async_save(loaded)

        if not isinstance(loaded, dict) or not loaded.get("webhook_id") or not loaded.get("verify_token"):
            if not create_if_missing:
                return None
            loaded = {
                "webhook_id": webhook.async_generate_id(),
                "verify_token": secrets.token_urlsafe(32),
                "subscription_id": None,
                "callback_url": None,
            }
            await self.store.async_save(loaded)

        self.record = loaded
        sub_id = parse_strava_id(loaded.get("subscription_id"))
        self.receiver = WebhookReceiver(
            subscription_id=sub_id,
            verify_token=loaded["verify_token"],
            targets=self.targets,
        )
        return self.record

    async def _resolve_callback_url(self, custom_callback_url: str | None = None) -> str:
        """Determine the public HTTPS webhook callback URL."""
        if custom_callback_url and custom_callback_url.strip():
            return validate_callback_url(custom_callback_url)

        assert self.record
        webhook_id = self.record["webhook_id"]
        try:
            url = webhook.async_generate_url(
                self.hass, webhook_id, allow_internal=False
            )
            return validate_callback_url(url)
        except (NoURLAvailableError, ValueError):
            pass

        if "cloud" in self.hass.config.components:
            from homeassistant.components import cloud

            if cloud.async_is_logged_in(self.hass):
                cloud_url = await cloud.async_get_or_create_cloudhook(self.hass, webhook_id)
                return validate_callback_url(cloud_url)

        raise NoURLAvailableError

    async def _api_request(
        self, method: str, subscription_id: int | str | None = None, **fields: Any
    ) -> Any:
        """Call Strava push_subscriptions endpoint without leaking secrets in logs."""
        url = SUBSCRIPTIONS_URL + (f"/{subscription_id}" if subscription_id is not None else "")
        values = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            **fields,
        }
        request_kwargs: dict[str, Any] = (
            {"params": values} if method in ("GET", "DELETE") else {"data": values}
        )
        try:
            async with asyncio.timeout(20):
                async with async_get_clientsession(self.hass).request(
                    method, url, allow_redirects=False, **request_kwargs
                ) as response:
                    if response.status == 204:
                        return None
                    if response.status not in (200, 201):
                        body_text = ""
                        try:
                            body_text = redact_secrets(await response.text())
                        except (ClientError, UnicodeDecodeError):
                            body_text = ""
                        raise SubscriptionError(
                            f"Strava subscription API {method} failed with HTTP {response.status}: {body_text[:300]}"
                        )
                    return await response.json()
        except SubscriptionError:
            raise
        except (ClientError, TimeoutError, ValueError) as err:
            raise SubscriptionError(
                redact_secrets(f"Strava subscription API {method} error: {type(err).__name__}")
            ) from None

    def _register_local_webhook(self) -> None:
        """Register the HA webhook route if not already registered."""
        if self.registered or not self.record or not self.receiver:
            return
        webhook.async_register(
            self.hass,
            DOMAIN,
            "Strava Webhook",
            self.record["webhook_id"],
            self.receiver.handle,
            local_only=False,
            allowed_methods=("GET", "POST"),
        )
        self.registered = True

    def _unregister_local_webhook(self) -> None:
        """Unregister the HA webhook route if registered."""
        if not self.registered or not self.record:
            return
        try:
            webhook.async_unregister(self.hass, self.record["webhook_id"])
        except (KeyError, ValueError) as err:
            _LOGGER.debug("Ignoring webhook unregister error: %s", err)
        self.registered = False

    async def _ensure_remote_subscription(
        self, callback_url: str, *, replace_existing: bool
    ) -> int:
        """Ensure Strava push subscription points to our callback_url."""
        assert self.receiver is not None
        records = await self._api_request("GET")
        if not isinstance(records, list):
            raise SubscriptionError("Invalid Strava push_subscriptions GET response")

        normalized_target = _normalize_url(callback_url)
        matched_id: int | None = None

        for item in records:
            if not isinstance(item, dict):
                continue
            item_id = parse_strava_id(item.get("id"))
            item_url = item.get("callback_url", "")
            if item_id is None:
                continue
            if _normalize_url(item_url) == normalized_target:
                matched_id = item_id
                break
            if replace_existing:
                _LOGGER.info(
                    "Deleting conflicting Strava webhook subscription id=%s for URL %s",
                    item_id,
                    item_url,
                )
                await self._api_request("DELETE", subscription_id=item_id)
            else:
                raise SubscriptionConflictError(item_url, callback_url)

        if matched_id is None:
            # Allow incoming GET challenge before subscription_id is assigned.
            self.receiver.subscription_id = None
            created = await self._api_request(
                "POST",
                callback_url=callback_url,
                verify_token=self.record["verify_token"],
            )
            matched_id = parse_strava_id(created.get("id")) if isinstance(created, dict) else None
            if matched_id is None:
                raise SubscriptionError("Strava did not return a valid subscription ID")

        self.record = {
            **self.record,
            "subscription_id": str(matched_id),
            "callback_url": callback_url,
        }
        await self.store.async_save(self.record)
        self.receiver.subscription_id = matched_id
        return matched_id

    async def async_connect(
        self,
        athlete_id: int,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        custom_callback_url: str | None = None,
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        """Connect an athlete handler and ensure webhook + Strava subscription are active."""
        async with self.lock:
            await self._ensure_loaded(create_if_missing=True)
            assert self.record is not None and self.receiver is not None

            callback_url = await self._resolve_callback_url(custom_callback_url)
            self.targets[athlete_id] = handler
            self._register_local_webhook()

            try:
                sub_id = await self._ensure_remote_subscription(
                    callback_url, replace_existing=replace_existing
                )
            except Exception:
                self.targets.pop(athlete_id, None)
                if not self.targets:
                    self._unregister_local_webhook()
                raise

            return {
                "subscription_id": sub_id,
                "callback_url": callback_url,
                "webhook_id": self.record["webhook_id"],
            }

    async def async_disconnect(self, athlete_id: int) -> None:
        """Detach an athlete handler on unload without deleting the remote subscription."""
        async with self.lock:
            self.targets.pop(athlete_id, None)
            if not self.targets:
                self._unregister_local_webhook()

    async def async_resync(
        self,
        *,
        custom_callback_url: str | None = None,
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        """Re-verify and ensure remote subscription is active."""
        async with self.lock:
            await self._ensure_loaded(create_if_missing=True)
            assert self.record is not None
            callback_url = await self._resolve_callback_url(custom_callback_url)
            self._register_local_webhook()
            sub_id = await self._ensure_remote_subscription(
                callback_url, replace_existing=replace_existing
            )
            return {
                "subscription_id": sub_id,
                "callback_url": callback_url,
                "webhook_id": self.record["webhook_id"],
            }

    async def async_delete_remote_subscription(self) -> bool:
        """Delete this integration's remote Strava subscription and local store."""
        async with self.lock:
            self._unregister_local_webhook()
            self.targets.clear()

            record = await self._ensure_loaded(create_if_missing=False)
            stored_sub_id = parse_strava_id(record.get("subscription_id")) if record else None
            stored_url = _normalize_url(record.get("callback_url")) if record else ""

            deleted_any = False
            try:
                records = await self._api_request("GET")
                if isinstance(records, list):
                    for item in records:
                        if not isinstance(item, dict):
                            continue
                        item_id = parse_strava_id(item.get("id"))
                        item_url = _normalize_url(item.get("callback_url"))
                        if item_id is None:
                            continue
                        matches_id = stored_sub_id is not None and item_id == stored_sub_id
                        matches_url = bool(stored_url) and item_url == stored_url
                        if matches_id or matches_url:
                            await self._api_request("DELETE", subscription_id=item_id)
                            deleted_any = True
                            _LOGGER.info(
                                "Deleted Strava push subscription id=%s on integration removal",
                                item_id,
                            )
                if not deleted_any and stored_sub_id is not None:
                    # In case GET didn't return it or returned stale list, attempt direct delete if none matched
                    pass
            finally:
                try:
                    await self.store.async_remove()
                except OSError as err:
                    _LOGGER.debug("Ignoring subscription store removal error: %s", err)
                self.record = {}
                self.receiver = None

            return deleted_any


async def async_get_subscription_manager(
    hass: HomeAssistant, client_id: str, client_secret: str
) -> SubscriptionManager:
    """Get or create the shared SubscriptionManager for a Strava API application."""
    managers: dict[str, SubscriptionManager] = hass.data.setdefault(
        f"{DOMAIN}_subscriptions", {}
    )
    client_key = str(client_id)
    manager = managers.get(client_key)
    if manager is None:
        store: Store[dict[str, Any]] = Store(
            hass,
            1,
            f"{DOMAIN}.app.{client_key}.subscription",
            private=True,
            atomic_writes=True,
        )
        manager = SubscriptionManager(hass, client_key, client_secret, store)
        managers[client_key] = manager
    else:
        manager.client_secret = client_secret
    return manager
