"""Pytest fixtures and lightweight Home Assistant stubs for local unit tests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
import sys
import types
from unittest.mock import MagicMock

import aiohttp
import voluptuous as vol

if not hasattr(asyncio, "timeout"):
    @asynccontextmanager
    async def _asyncio_timeout(_seconds):
        yield

    asyncio.timeout = _asyncio_timeout

try:
    from enum import StrEnum
except ImportError:
    class StrEnum(str, Enum):
        pass


def _install_ha_stubs() -> None:
    """Install minimal homeassistant module stubs if homeassistant is not installed."""
    if "homeassistant" in sys.modules:
        return

    ha = types.ModuleType("homeassistant")
    sys.modules["homeassistant"] = ha

    # homeassistant.const
    const = types.ModuleType("homeassistant.const")

    class Platform(StrEnum):
        EVENT = "event"
        SENSOR = "sensor"

    class EntityCategory(StrEnum):
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    const.Platform = Platform
    const.EntityCategory = EntityCategory
    const.CONF_DEVICE_ID = "device_id"
    const.CONF_DOMAIN = "domain"
    const.CONF_PLATFORM = "platform"
    const.CONF_TYPE = "type"
    sys.modules["homeassistant.const"] = const

    # homeassistant.core
    core = types.ModuleType("homeassistant.core")

    class HomeAssistant:
        def __init__(self) -> None:
            self.data = {}
            self.config = types.SimpleNamespace(components=set())
            self.bus = MagicMock()
            self.services = MagicMock()
            self.config_entries = MagicMock()

        async def async_run_hass_job(self, job, *args):
            return await job.target(*args)

    class HassJob:
        def __init__(self, target):
            self.target = target

    def callback(func):
        return func

    core.HomeAssistant = HomeAssistant
    core.HassJob = HassJob
    core.CALLBACK_TYPE = object
    core.ServiceCall = object
    core.callback = callback
    sys.modules["homeassistant.core"] = core

    # homeassistant.exceptions
    exc = types.ModuleType("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        pass

    class ConfigEntryNotReady(HomeAssistantError):
        pass

    class ConfigEntryAuthFailed(HomeAssistantError):
        pass

    class ServiceValidationError(HomeAssistantError):
        pass

    class OAuth2TokenRequestConnectionError(HomeAssistantError):
        def __init__(self, domain=None):
            super().__init__(domain)

    class OAuth2TokenRequestReauthError(HomeAssistantError):
        def __init__(self, domain=None, request_info=None, status=None):
            super().__init__(domain)
            self.status = status

    exc.HomeAssistantError = HomeAssistantError
    exc.ConfigEntryNotReady = ConfigEntryNotReady
    exc.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    exc.ServiceValidationError = ServiceValidationError
    exc.OAuth2TokenRequestConnectionError = OAuth2TokenRequestConnectionError
    exc.OAuth2TokenRequestReauthError = OAuth2TokenRequestReauthError
    sys.modules["homeassistant.exceptions"] = exc

    # homeassistant.config_entries
    ce = types.ModuleType("homeassistant.config_entries")
    ce.SOURCE_REAUTH = "reauth"

    class ConfigEntry:
        pass

    class OptionsFlow:
        pass

    ce.ConfigEntry = ConfigEntry
    ce.OptionsFlow = OptionsFlow
    sys.modules["homeassistant.config_entries"] = ce

    # homeassistant.data_entry_flow
    def_mod = types.ModuleType("homeassistant.data_entry_flow")
    def_mod.FlowResult = dict
    sys.modules["homeassistant.data_entry_flow"] = def_mod

    # homeassistant.util.dt
    util = types.ModuleType("homeassistant.util")
    dt_mod = types.ModuleType("homeassistant.util.dt")
    from datetime import datetime, timezone

    dt_mod.utcnow = lambda: datetime.now(timezone.utc)
    dt_mod.now = lambda: datetime.now(timezone.utc)
    util.dt = dt_mod
    sys.modules["homeassistant.util"] = util
    sys.modules["homeassistant.util.dt"] = dt_mod

    # homeassistant.helpers.*
    helpers = types.ModuleType("homeassistant.helpers")
    sys.modules["homeassistant.helpers"] = helpers

    cv = types.ModuleType("homeassistant.helpers.config_validation")
    cv.string = vol.Coerce(str)
    cv.config_entry_only_config_schema = lambda domain: vol.Schema({})
    helpers.config_validation = cv
    sys.modules["homeassistant.helpers.config_validation"] = cv

    aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda hass: hass.data.get("_test_session")
    sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client

    network = types.ModuleType("homeassistant.helpers.network")

    class NoURLAvailableError(Exception):
        pass

    network.NoURLAvailableError = NoURLAvailableError
    sys.modules["homeassistant.helpers.network"] = network

    storage = types.ModuleType("homeassistant.helpers.storage")

    class Store:
        def __init__(self, hass, version, key, **kwargs):
            self.hass = hass
            self.key = key
            self._data = None

        async def async_load(self):
            return self._data

        async def async_save(self, data):
            self._data = data

        async def async_remove(self):
            self._data = None

    storage.Store = Store
    sys.modules["homeassistant.helpers.storage"] = storage

    dr = types.ModuleType("homeassistant.helpers.device_registry")

    class DeviceEntryType(StrEnum):
        SERVICE = "service"

    class DeviceInfo(dict):
        pass

    dr.DeviceEntryType = DeviceEntryType
    dr.DeviceInfo = DeviceInfo
    dr.async_get = MagicMock()
    sys.modules["homeassistant.helpers.device_registry"] = dr

    ep = types.ModuleType("homeassistant.helpers.entity_platform")
    ep.AddEntitiesCallback = object
    sys.modules["homeassistant.helpers.entity_platform"] = ep

    typing_mod = types.ModuleType("homeassistant.helpers.typing")
    typing_mod.ConfigType = dict
    sys.modules["homeassistant.helpers.typing"] = typing_mod

    trig = types.ModuleType("homeassistant.helpers.trigger")
    trig.TriggerActionType = object
    trig.TriggerInfo = dict
    sys.modules["homeassistant.helpers.trigger"] = trig

    sel = types.ModuleType("homeassistant.helpers.selector")

    class SelectSelectorMode(StrEnum):
        DROPDOWN = "dropdown"

    @dataclass
    class SelectSelectorConfig:
        options: list
        multiple: bool = False
        custom_value: bool = False
        translation_key: str | None = None
        mode: str = SelectSelectorMode.DROPDOWN

    class SelectSelector:
        def __init__(self, config: SelectSelectorConfig) -> None:
            self.config = config

        def __call__(self, value):
            return value

    sel.SelectSelectorMode = SelectSelectorMode
    sel.SelectSelectorConfig = SelectSelectorConfig
    sel.SelectSelector = SelectSelector
    sys.modules["homeassistant.helpers.selector"] = sel

    oauth2 = types.ModuleType("homeassistant.helpers.config_entry_oauth2_flow")

    class AbstractOAuth2FlowHandler:
        def __init_subclass__(cls, domain=None, **kwargs):
            cls.DOMAIN = domain

    class OAuth2Session:
        def __init__(self, hass, entry, impl):
            self.hass = hass
            self.entry = entry
            self.implementation = impl
            self.token = {"access_token": "test-access-token"}

        async def async_ensure_token_valid(self):
            return None

    oauth2.AbstractOAuth2FlowHandler = AbstractOAuth2FlowHandler
    oauth2.OAuth2Session = OAuth2Session
    oauth2.async_get_config_entry_implementation = MagicMock()
    sys.modules["homeassistant.helpers.config_entry_oauth2_flow"] = oauth2

    # homeassistant.components.*
    comps = types.ModuleType("homeassistant.components")
    sys.modules["homeassistant.components"] = comps

    app_creds = types.ModuleType("homeassistant.components.application_credentials")

    @dataclass
    class AuthorizationServer:
        authorize_url: str
        token_url: str

    @dataclass
    class ClientCredential:
        client_id: str
        client_secret: str

    class AuthImplementation:
        def __init__(self, hass, domain, credential, authorization_server):
            self.hass = hass
            self.domain = domain
            self.client_id = credential.client_id
            self.client_secret = credential.client_secret
            self.token_url = authorization_server.token_url

    app_creds.AuthorizationServer = AuthorizationServer
    app_creds.ClientCredential = ClientCredential
    app_creds.AuthImplementation = AuthImplementation
    sys.modules["homeassistant.components.application_credentials"] = app_creds

    wh = types.ModuleType("homeassistant.components.webhook")
    wh.async_generate_id = lambda: "wh_generated_id_1234567890"
    wh.async_generate_url = lambda hass, webhook_id, allow_internal=False: (
        f"https://ha.example.com/api/webhook/{webhook_id}"
    )
    wh.async_register = MagicMock()
    wh.async_unregister = MagicMock()
    sys.modules["homeassistant.components.webhook"] = wh

    da = types.ModuleType("homeassistant.components.device_automation")
    da.DEVICE_TRIGGER_BASE_SCHEMA = vol.Schema(
        {
            vol.Required("platform"): "device",
            vol.Required("domain"): str,
            vol.Required("device_id"): str,
        }
    )
    sys.modules["homeassistant.components.device_automation"] = da

    ev_comp = types.ModuleType("homeassistant.components.event")

    class EventEntity:
        def _trigger_event(self, event_type, attrs=None):
            self._last_event = (event_type, attrs)

        def async_write_ha_state(self):
            pass

        async def async_added_to_hass(self):
            pass

        async def async_will_remove_from_hass(self):
            pass

    ev_comp.EventEntity = EventEntity
    sys.modules["homeassistant.components.event"] = ev_comp

    sens_comp = types.ModuleType("homeassistant.components.sensor")

    class SensorEntity:
        def async_write_ha_state(self):
            pass

        async def async_added_to_hass(self):
            pass

        async def async_will_remove_from_hass(self):
            pass

    sens_comp.SensorEntity = SensorEntity
    sys.modules["homeassistant.components.sensor"] = sens_comp


_install_ha_stubs()
