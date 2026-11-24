"""Constants for the Strava Webhook integration."""

from __future__ import annotations

import re

from homeassistant.const import Platform

DOMAIN = "strava_webhook"

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE_URL = "https://www.strava.com/api/v3"
ATHLETE_URL = f"{API_BASE_URL}/athlete"
ACTIVITIES_URL = f"{API_BASE_URL}/activities"
ATHLETE_ACTIVITIES_URL = f"{API_BASE_URL}/athlete/activities"
SUBSCRIPTIONS_URL = f"{API_BASE_URL}/push_subscriptions"
STRAVA_ACTIVITY_URL_PREFIX = "https://www.strava.com/activities/"

OAUTH_SCOPES = "read,activity:read_all"

EVENT_STRAVA_WEBHOOK = "strava_webhook_event"

OBJECT_TYPES = ("activity", "athlete")
ASPECT_TYPES = ("create", "update", "delete")

CONF_ATHLETE_ID = "athlete_id"
CONF_ATHLETE_NAME = "athlete_name"
CONF_CLIENT_ID = "client_id"

CONF_ENRICH_ACTIVITIES = "enrich_activities"
CONF_CUSTOM_CALLBACK_URL = "custom_callback_url"
CONF_REPLACE_EXISTING_SUBSCRIPTION = "replace_existing_subscription"

DEFAULT_ENRICH_ACTIVITIES = True
DEFAULT_REPLACE_EXISTING_SUBSCRIPTION = False

SERVICE_RESYNC_SUBSCRIPTION = "resync_subscription"
SERVICE_SIMULATE_EVENT = "simulate_event"

PLATFORMS: list[Platform] = [Platform.EVENT, Platform.SENSOR]

# All 56 official Strava sport_type values.
SUPPORTED_SPORT_TYPES: tuple[str, ...] = (
    "AlpineSki",
    "BackcountrySki",
    "Badminton",
    "Basketball",
    "Canoeing",
    "Cricket",
    "Crossfit",
    "Dance",
    "EBikeRide",
    "Elliptical",
    "EMountainBikeRide",
    "Golf",
    "GravelRide",
    "Handcycle",
    "HighIntensityIntervalTraining",
    "Hike",
    "IceSkate",
    "InlineSkate",
    "Kayaking",
    "Kitesurf",
    "MountainBikeRide",
    "NordicSki",
    "Other",
    "Padel",
    "Pickleball",
    "Pilates",
    "Racquetball",
    "Ride",
    "RockClimbing",
    "RollerSki",
    "Rowing",
    "Run",
    "Sail",
    "Skateboard",
    "Snowboard",
    "Snowshoe",
    "Soccer",
    "Squash",
    "StairStepper",
    "StandUpPaddling",
    "Surfing",
    "Swim",
    "TableTennis",
    "Tennis",
    "TrailRun",
    "Velomobile",
    "VirtualRide",
    "VirtualRow",
    "VirtualRun",
    "Volleyball",
    "Walk",
    "WeightTraining",
    "Wheelchair",
    "Windsurf",
    "Workout",
    "Yoga",
)

LOG_REDACTED_PLACEHOLDER = "<redacted>"
LOG_REDACT_PATTERN = re.compile(
    r"\b(client_secret|access_token|refresh_token|code|verify_token)=['\"]?[^&\s'\"]*",
    re.IGNORECASE,
)


def redact_secrets(text: str) -> str:
    """Redact credentials or verify tokens from log or error messages."""
    return LOG_REDACT_PATTERN.sub(
        lambda match: f"{match.group(1)}={LOG_REDACTED_PLACEHOLDER}", text
    )
