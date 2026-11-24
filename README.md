# Strava Webhook for Home Assistant

A lightweight, UI-configurable Home Assistant custom integration that listens for **Strava Push Webhook** events (`create`, `update`, `delete` for activities and `update` for athlete deauthorization), optionally enriches activity webhooks with full activity metadata from the Strava REST API, exposes native **Automation UI Device Triggers** with sport/type filters, and fires a rich `strava_webhook_event` containing **100% of the raw webhook parameters** plus activity attributes.

## Features

- **100% UI Configuration (Config Flow + Options Flow)**: Uses Home Assistant's native **Application Credentials** and **OAuth2** flow (minimal read-only scopes: `read,activity:read_all`), with automatic token refresh and reauthentication support.
- **Automatic Push Subscription Lifecycle**: Automatically registers a secure Home Assistant `/api/webhook/<id>` endpoint, verifies Strava's `hub.challenge` handshake, creates the remote Strava Push Subscription, and **automatically deletes the remote subscription from Strava** when the integration entry is removed in Home Assistant.
- **All Webhook Parameters Preserved**: Every incoming webhook emits `strava_webhook_event` on the Home Assistant event bus with all original webhook keys (`aspect_type`, `event_time`, `object_id`, `object_type`, `owner_id`, `subscription_id`, `updates`, and any extra fields) plus an untouched `raw_webhook` dictionary.
- **Automatic Activity Enrichment**: Because Strava's `create` webhook payload does not include `sport_type` or activity title, the integration responds `200 OK` within milliseconds and asynchronously fetches full activity details (`GET /api/v3/activities/{id}`) with bounded retries.
- **Native Automation UI Device Triggers**: Exposes 5 device triggers in the Automation UI with optional filters across all **56 official Strava `sport_type` values**, `object_type`, and `aspect_type`.
- **Entities & Testing Tools**: Includes an `EventEntity` (`Last webhook event`), a diagnostic `SensorEntity` (`Webhook status`), and `strava_webhook.simulate_event` / `strava_webhook.resync_subscription` services.
- **Multilingual UI**: Full **English** and **Polish** (`pl`) translations, plus official Strava brand icons and logos.

---

## Requirements

1. **Public HTTPS access to Home Assistant**: Strava must be able to reach your Home Assistant instance over public HTTPS (via External URL + reverse proxy/tunnel, or Home Assistant Cloud).
2. **Personal Strava API Application**: Create an app at [strava.com/settings/api](https://www.strava.com/settings/api) and set **Authorization Callback Domain** to `my.home-assistant.io`.
   - *Note:* Strava allows at most **one** active webhook subscription per `Client ID`.

---

## Installation

### Option 1: HACS (Recommended)

1. Open **HACS** in Home Assistant.
2. Open the top-right menu (`⋮`) → **Custom repositories**.
3. Add this repository URL: `https://github.com/cpiekacz/ha-strava-webhook` and select **Integration** as the category.
4. Search for **Strava Webhook**, download it, and **restart Home Assistant**.

### Option 2: Manual Installation

1. Copy the `custom_components/strava_webhook` folder into your Home Assistant `<config>/custom_components/` directory so you have:
   ```text
   <config>/custom_components/strava_webhook/
     ├── __init__.py
     ├── manifest.json
     ├── ...
   ```
2. **Restart Home Assistant**.

---

## Configuration

1. Go to **Settings → Devices & services → Add integration → Strava Webhook**.
2. When prompted by Home Assistant's **Application Credentials** dialog, enter any friendly label (e.g. `Strava`) along with your Strava **Client ID** and **Client Secret**.
3. Complete the OAuth2 sign-in on Strava and approve `read` + `activity:read_all`.
4. Verify the integration's **Webhook status** diagnostic sensor shows **Connected** with an active `subscription_id`.

### Options

Click **Configure** on the integration entry to adjust:
- **Fetch full activity details from Strava API** (default: `true`): Enriches `create`/`update` activity webhooks with `sport_type`, `name`, `distance`, `moving_time`, `device_name`, etc.
- **Custom public webhook URL** (optional): Override Home Assistant's default External URL with a custom HTTPS callback URL.
- **Replace existing Strava subscription on URL conflict** (default: `false`): Automatically deletes an existing Strava subscription on the same `Client ID` if it points to a different callback URL.

---

## Event Payload (`strava_webhook_event`)

Every received webhook fires `strava_webhook_event` on the Home Assistant event bus:

- **Raw Strava webhook fields (top-level + `raw_webhook`)**:
  `aspect_type` (`create` | `update` | `delete`), `event_time`, `object_id`, `object_type` (`activity` | `athlete`), `owner_id`, `subscription_id`, `updates` (dict), and any additional fields sent by Strava.
- **Home Assistant metadata**:
  `config_entry_id`, `device_id`, `athlete_id`, `enriched` (`true`/`false`).
- **Flattened activity fields** (when `object_type == "activity"` and `enriched == true`):
  `activity_id`, `activity_url`, `sport_type`, `type`, `name`, `description`, `distance`, `moving_time`, `elapsed_time`, `total_elevation_gain`, `start_date`, `start_date_local`, `timezone`, `trainer`, `commute`, `manual`, `private`, `flagged`, `workout_type`, `device_name`, `gear_id`, `average_speed`, `max_speed`, `average_heartrate`, `max_heartrate`, `calories`, `suffer_score`, `external_id`, `upload_id`, `kudos_count`, `achievement_count`, `pr_count`, `location_city`, `location_state`, `location_country`, plus the `activity` dictionary.

---

## Automation Triggers & Example

### Device Triggers (UI)

Select your **Strava** device in the Automation Editor to choose from:
- **Activity created** (optional `sport_type` filter)
- **Activity updated** (optional `sport_type` filter)
- **Activity created or updated** (optional `sport_type` filter)
- **Activity deleted**
- **Any Strava webhook received** (optional `object_type`, `aspect_type`, and `sport_type` filters)

### Event Trigger Example (YAML)

```yaml
alias: "Strava: Strength Training Prompt"
mode: queued
triggers:
  - id: strava_gym
    trigger: event
    event_type: strava_webhook_event
    event_data:
      object_type: activity
      aspect_type: create
      sport_type: WeightTraining
actions:
  - action: notify.mobile_app_my_phone
    data:
      title: "New Strength Training on Strava"
      message: "Logged {{ trigger.event.data.name | default('Weight Training') }}"
```

### Services

- `strava_webhook.simulate_event`: Fires a simulated `strava_webhook_event` (defaults to `activity` / `create` / `WeightTraining`) to test your automations without completing a real workout.
- `strava_webhook.resync_subscription`: Re-checks and repairs the remote Strava Push Subscription.

---

## Development & Tests

```bash
python3 -m venv .venv
.venv/bin/pip install pytest pytest-asyncio aiohttp voluptuous yarl
.venv/bin/pytest -v
```
