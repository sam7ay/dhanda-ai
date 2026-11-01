"""Thin client for the Sarvam Voice Agents API (campaigns, cohorts, instant calls, analytics).

Docs: https://docs.sarvam.ai/voice-agents/api/introduction
"""
import os
from datetime import datetime, timedelta, timezone

import httpx

BASE = "https://apps.sarvam.ai/api"
REQUIRED = {
    "SARVAM_VA_API_KEY": "Voice Agents API key (dashboard → Settings → API Key)",
    "SARVAM_ORG_ID": "Organisation ID (from the dashboard URL or Settings)",
    "SARVAM_WORKSPACE_ID": "Workspace ID (from the dashboard URL or Settings)",
    "SARVAM_APP_ID": "Agent ID (agent ··· menu → Copy agent ID)",
    "SARVAM_APP_VERSION": "Committed agent version number, e.g. 1",
    "SARVAM_CONNECTION_ID": "Phone connection ID (Deploy → Phone Numbers → Sarvam Vobiz)",
    "SARVAM_AGENT_NUMBER": "The rented number calls go out from, in +91 format",
}


class SarvamError(Exception):
    pass


def missing():
    return {k: v for k, v in REQUIRED.items() if not os.environ.get(k)}


def configured():
    return not missing()


def _cfg(key):
    return os.environ[key]


def _scope():
    return f"orgs/{_cfg('SARVAM_ORG_ID')}/workspaces/{_cfg('SARVAM_WORKSPACE_ID')}"


def _request(method, url, **kwargs):
    headers = {"X-API-Key": _cfg("SARVAM_VA_API_KEY")}
    try:
        resp = httpx.request(method, url, headers=headers, timeout=30, **kwargs)
    except httpx.HTTPError as exc:
        raise SarvamError(f"Could not reach Sarvam: {exc}") from exc
    if resp.status_code >= 400:
        raise SarvamError(f"Sarvam returned {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def _connection():
    return {"connection_id": _cfg("SARVAM_CONNECTION_ID"), "agent_phone_number": _cfg("SARVAM_AGENT_NUMBER")}


def _webhook(public_base_url, metadata):
    if not public_base_url:
        return None
    return {"url": public_base_url.rstrip("/") + "/webhooks/sarvam", "metadata": metadata}


def create_campaign(name, window_start, window_end, days, public_base_url=None, metadata=None):
    now = datetime.now(timezone.utc)
    app_config = {
        "app_id": _cfg("SARVAM_APP_ID"),
        "app_type": "agent",
        "app_version": int(_cfg("SARVAM_APP_VERSION")),
        "attempts_per_second": 1,
        "connection_configs": [{**_connection(), "phone_numbers": [_cfg("SARVAM_AGENT_NUMBER")], "weight": 1}],
        "retry_config": {
            "max_retries": 2,
            "retry_interval_minutes": 60,
            "retry_on": {"busy": {"enabled": True}, "no_answer": {"enabled": True}},
        },
    }
    webhook = _webhook(public_base_url, metadata or {})
    if webhook:
        app_config["webhook_config"] = webhook
    body = {
        "name": name[:100],
        "description": "Dhanda AI payment reminders",
        "app_config": app_config,
        "start_timestamp": (now + timedelta(minutes=2)).isoformat(),
        "end_timestamp": (now + timedelta(days=days)).isoformat(),
        "allowed_schedule": {
            "allowed_start_time": window_start,
            "allowed_end_time": window_end,
            "allowed_days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
            "timezone": "Asia/Kolkata",
        },
    }
    return _request("POST", f"{BASE}/scheduling/v1/{_scope()}/campaigns", json=body)


def stream_cohort(campaign_id, name, users):
    """users: [{user_phone_number, user_identifier, app_variables, app_overrides}] (max 1000)."""
    return _request("POST", f"{BASE}/scheduling/v1/{_scope()}/campaigns/{campaign_id}/cohorts/stream",
                    json={"name": name[:50], "users": users})


def get_campaign(campaign_id):
    return _request("GET", f"{BASE}/scheduling/v1/{_scope()}/campaigns/{campaign_id}")


def set_campaign_status(campaign_id, action):
    return _request("PUT", f"{BASE}/scheduling/v1/{_scope()}/campaigns/{campaign_id}/status",
                    json={"action": action})


def instant_call(phone, variables, language, public_base_url=None, metadata=None):
    body = {
        "app_config": {
            "app_id": _cfg("SARVAM_APP_ID"),
            "app_version": int(_cfg("SARVAM_APP_VERSION")),
            "connection_config": _connection(),
            "agent_variables": variables,
            "app_overrides": {"initial_language_name": language},
        },
        "user_config": {"user_phone_number": phone},
    }
    webhook = _webhook(public_base_url, metadata or {})
    if webhook:
        body["webhook_config"] = webhook
    return _request("POST", f"{BASE}/outbounds/v1/{_scope()}/outbounds", json=body)


def list_attempts(since):
    """All attempts for the agent from `since` until now, following pagination."""
    url = f"{BASE}/analytics/v1/{_cfg('SARVAM_ORG_ID')}/{_cfg('SARVAM_WORKSPACE_ID')}/{_cfg('SARVAM_APP_ID')}/attempts"
    params = {
        "start_datetime": since.astimezone(timezone.utc).isoformat(),
        "end_datetime": datetime.now(timezone.utc).isoformat(),
        "limit": 200,
        "offset": 0,
        "sort_by": "attempted_at",
    }
    items = []
    while True:
        page = _request("GET", url, params=params)
        items.extend(page.get("items", []))
        if not page.get("next_page_uri") or len(items) >= page.get("total", 0):
            return items
        params["offset"] += params["limit"]


def get_transcript(interaction_id):
    """Returns [{role: 'agent'|'user', text}] for one connected call."""
    url = (f"{BASE}/analytics/v1/{_cfg('SARVAM_ORG_ID')}/{_cfg('SARVAM_WORKSPACE_ID')}/"
           f"{_cfg('SARVAM_APP_ID')}/transcripts/{interaction_id}")
    messages = _request("GET", url).get("messages", [])
    return [{"role": "agent" if m["role"] == "assistant" else "user", "text": m["content"]} for m in messages]
