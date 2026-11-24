"""Tests for Strava webhook event payload builder."""

from custom_components.strava_webhook.event_builder import (
    build_event_entity_attributes,
    build_webhook_event_payload,
    determine_entity_event_type,
)


def test_build_payload_preserves_all_raw_fields_and_enriches_activity():
    raw_webhook = {
        "aspect_type": "create",
        "event_time": 1732473819,
        "object_id": 12345678901,
        "object_type": "activity",
        "owner_id": 98765432,
        "subscription_id": 555111,
        "updates": {},
        "future_strava_field": "kept_intact",
    }
    activity = {
        "id": 12345678901,
        "name": "Evening Lift",
        "sport_type": "WeightTraining",
        "type": "WeightTraining",
        "distance": 0.0,
        "moving_time": 3200,
        "elapsed_time": 3400,
        "device_name": "Garmin Fenix 7",
        "private": False,
        "average_heartrate": 121.5,
        "segment_efforts": [{"id": 1}, {"id": 2}],
        "laps": [{"id": 9}],
    }

    payload = build_webhook_event_payload(
        raw_webhook,
        config_entry_id="entry-1",
        device_id="dev-1",
        athlete_id=98765432,
        activity=activity,
        enriched=True,
    )

    assert payload["aspect_type"] == "create"
    assert payload["event_time"] == 1732473819
    assert payload["object_id"] == 12345678901
    assert payload["object_type"] == "activity"
    assert payload["owner_id"] == 98765432
    assert payload["subscription_id"] == 555111
    assert payload["future_strava_field"] == "kept_intact"
    assert payload["raw_webhook"] == raw_webhook
    assert payload["config_entry_id"] == "entry-1"
    assert payload["device_id"] == "dev-1"
    assert payload["athlete_id"] == "98765432"
    assert payload["enriched"] is True
    assert payload["activity_id"] == 12345678901
    assert payload["activity_url"] == "https://www.strava.com/activities/12345678901"
    assert payload["sport_type"] == "WeightTraining"
    assert payload["name"] == "Evening Lift"
    assert payload["device_name"] == "Garmin Fenix 7"
    assert "segment_efforts" not in payload["activity"]
    assert "laps" not in payload["activity"]
    assert payload["activity"]["average_heartrate"] == 121.5


def test_build_payload_promotes_updates_when_unenriched():
    raw_webhook = {
        "aspect_type": "update",
        "event_time": 1732473900,
        "object_id": 12345678901,
        "object_type": "activity",
        "owner_id": 98765432,
        "subscription_id": 555111,
        "updates": {"title": "Renamed Run", "type": "Run", "private": "true"},
    }

    payload = build_webhook_event_payload(
        raw_webhook,
        config_entry_id="entry-1",
        device_id="dev-1",
        athlete_id=98765432,
        activity=None,
        enriched=False,
    )

    assert payload["enriched"] is False
    assert payload["name"] == "Renamed Run"
    assert payload["sport_type"] == "Run"
    assert payload["type"] == "Run"
    assert payload["private"] is True
    assert payload["updates"] == raw_webhook["updates"]


def test_event_entity_attributes_and_type_mapping():
    assert determine_entity_event_type("activity", "create") == "activity_created"
    assert determine_entity_event_type("activity", "update") == "activity_updated"
    assert determine_entity_event_type("activity", "delete") == "activity_deleted"
    assert determine_entity_event_type("athlete", "update") == "athlete_updated"
    assert determine_entity_event_type("other", "x") == "webhook_received"

    attrs = build_event_entity_attributes(
        {
            "aspect_type": "create",
            "sport_type": "WeightTraining",
            "activity": {"huge": "blob"},
            "raw_webhook": {"a": 1},
        }
    )
    assert attrs["sport_type"] == "WeightTraining"
    assert "activity" not in attrs
    assert "raw_webhook" not in attrs
