"""Builds HA event bus and EventEntity payloads from Strava webhooks and activities."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .const import STRAVA_ACTIVITY_URL_PREFIX

# Very large nested arrays from DetailedActivity are omitted from the HA event payload
# so we preserve all useful metadata without bloating HA's event bus or recorder DB.
ACTIVITY_HEAVY_KEYS: frozenset[str] = frozenset(
    {
        "segment_efforts",
        "splits_metric",
        "splits_standard",
        "laps",
        "best_efforts",
        "similar_activities",
        "available_zones",
        "highlighted_kudosers",
    }
)

# Scalar/summary fields promoted to the top level of strava_webhook_event for easy filtering.
PROMOTED_ACTIVITY_KEYS: tuple[str, ...] = (
    "sport_type",
    "type",
    "name",
    "description",
    "distance",
    "moving_time",
    "elapsed_time",
    "total_elevation_gain",
    "start_date",
    "start_date_local",
    "timezone",
    "trainer",
    "commute",
    "manual",
    "private",
    "flagged",
    "workout_type",
    "device_name",
    "gear_id",
    "average_speed",
    "max_speed",
    "average_heartrate",
    "max_heartrate",
    "calories",
    "suffer_score",
    "external_id",
    "upload_id",
    "kudos_count",
    "achievement_count",
    "pr_count",
    "location_city",
    "location_state",
    "location_country",
)

# Keys reserved for HA integration metadata so raw_webhook extras never clobber them.
RESERVED_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "raw_webhook",
        "config_entry_id",
        "device_id",
        "athlete_id",
        "enriched",
        "activity",
        "activity_id",
        "activity_url",
    }
)


def sanitize_activity_dict(activity: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the Strava activity dict excluding heavy array fields."""
    return {
        key: deepcopy(value)
        for key, value in activity.items()
        if key not in ACTIVITY_HEAVY_KEYS
    }


def build_webhook_event_payload(
    raw_webhook: dict[str, Any],
    *,
    config_entry_id: str,
    device_id: str | None,
    athlete_id: int | str,
    activity: dict[str, Any] | None = None,
    enriched: bool = False,
) -> dict[str, Any]:
    """Combine all raw Strava webhook parameters with optional enriched activity data."""
    payload: dict[str, Any] = {}

    # 1. Preserve 100% of original webhook parameters at top level + intact raw_webhook copy.
    for key, value in raw_webhook.items():
        if key not in RESERVED_METADATA_KEYS:
            payload[key] = deepcopy(value)

    if "updates" not in payload or not isinstance(payload["updates"], dict):
        payload["updates"] = (
            deepcopy(raw_webhook.get("updates"))
            if isinstance(raw_webhook.get("updates"), dict)
            else {}
        )

    payload["raw_webhook"] = deepcopy(raw_webhook)

    # 2. Integration metadata.
    payload["config_entry_id"] = config_entry_id
    payload["device_id"] = device_id
    payload["athlete_id"] = str(athlete_id)
    payload["enriched"] = enriched

    object_type = raw_webhook.get("object_type")
    object_id = raw_webhook.get("object_id")
    updates = payload["updates"]

    # 3. Activity enrichment / fallback promotion from updates.
    if object_type == "activity" and object_id is not None:
        payload["activity_id"] = object_id
        payload["activity_url"] = f"{STRAVA_ACTIVITY_URL_PREFIX}{object_id}"

        if activity is not None:
            cleaned = sanitize_activity_dict(activity)
            for key in PROMOTED_ACTIVITY_KEYS:
                if key in cleaned:
                    payload[key] = cleaned[key]
            # Ensure sport_type falls back to legacy type if absent.
            if not payload.get("sport_type") and cleaned.get("type"):
                payload["sport_type"] = cleaned["type"]
            payload["activity"] = cleaned
        else:
            # Even when unenriched (delete or enrich disabled), promote fields from updates if present.
            if "sport_type" in updates:
                payload["sport_type"] = updates["sport_type"]
            elif "type" in updates:
                payload["sport_type"] = updates["type"]
                payload["type"] = updates["type"]
            if "type" in updates and "type" not in payload:
                payload["type"] = updates["type"]
            if "title" in updates:
                payload["name"] = updates["title"]
            if "private" in updates:
                private_val = updates["private"]
                if isinstance(private_val, str):
                    payload["private"] = private_val.lower() == "true"
                else:
                    payload["private"] = bool(private_val)

    return payload


def build_event_entity_attributes(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a compact attribute dict safe for HA EventEntity state attributes (< 16 KB)."""
    compact_keys = (
        "aspect_type",
        "event_time",
        "object_id",
        "object_type",
        "owner_id",
        "subscription_id",
        "updates",
        "athlete_id",
        "enriched",
        "activity_id",
        "activity_url",
        "sport_type",
        "type",
        "name",
        "distance",
        "moving_time",
        "elapsed_time",
        "total_elevation_gain",
        "start_date_local",
        "trainer",
        "commute",
        "manual",
        "private",
        "device_name",
        "calories",
        "average_heartrate",
        "max_heartrate",
    )
    return {
        key: deepcopy(payload[key])
        for key in compact_keys
        if key in payload
    }


def determine_entity_event_type(object_type: str | None, aspect_type: str | None) -> str:
    """Map Strava webhook object_type and aspect_type to EventEntity event_type."""
    if object_type == "activity":
        if aspect_type == "create":
            return "activity_created"
        if aspect_type == "update":
            return "activity_updated"
        if aspect_type == "delete":
            return "activity_deleted"
    if object_type == "athlete":
        return "athlete_updated"
    return "webhook_received"
