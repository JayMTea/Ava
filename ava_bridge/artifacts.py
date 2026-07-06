"""Chat artifacts — Claude-style side-panel visualizations for a turn.

When a turn uses a tool that has a rich visual companion (starting with
`get_weather`), we deterministically re-fetch the structured data host-side
from the same public source the tool uses (Open-Meteo, no API key) and attach a
compact, chartable `artifact` payload to the turn. The web UI opens a resizable
split "artifact" panel next to the chat and renders it — Ava still answers in
prose, and the panel adds an awesome visualization on the side.

This is deterministic (no LLM parsing of tool output): we read the tool-call
arguments the agent actually used from its session jsonl, then fetch fresh data
ourselves. If anything fails the artifact is simply omitted — it never blocks or
breaks a normal reply.
"""
import json
import os
import shlex

import requests

from . import config
from .agent import _sbx_read, _session_file

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather codes → (human description, emoji). Mirrors the get_weather tool.
WMO = {
    0: ("Clear sky", "☀️"), 1: ("Mainly clear", "🌤️"), 2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"), 45: ("Fog", "🌫️"), 48: ("Rime fog", "🌫️"),
    51: ("Light drizzle", "🌦️"), 53: ("Drizzle", "🌦️"), 55: ("Dense drizzle", "🌧️"),
    56: ("Freezing drizzle", "🌧️"), 57: ("Freezing drizzle", "🌧️"),
    61: ("Slight rain", "🌦️"), 63: ("Rain", "🌧️"), 65: ("Heavy rain", "🌧️"),
    66: ("Freezing rain", "🌧️"), 67: ("Freezing rain", "🌧️"),
    71: ("Slight snow", "🌨️"), 73: ("Snow", "🌨️"), 75: ("Heavy snow", "❄️"),
    77: ("Snow grains", "🌨️"),
    80: ("Rain showers", "🌦️"), 81: ("Rain showers", "🌧️"), 82: ("Heavy showers", "⛈️"),
    85: ("Snow showers", "🌨️"), 86: ("Heavy snow showers", "❄️"),
    95: ("Thunderstorm", "⛈️"), 96: ("Thunderstorm w/ hail", "⛈️"), 99: ("Severe thunderstorm", "⛈️"),
}

def _default_location_name() -> str:
    """Owner's configured default place for weather (blank = no default)."""
    v = os.environ.get("AVA_DEFAULT_LOCATION")
    if v:
        return v.strip()
    try:
        from . import settings
        return settings.owner_location()
    except Exception:  # noqa: BLE001
        return ""


def _wmo(code):
    return WMO.get(int(code) if code is not None else -1, ("Mixed conditions", "🌡️"))


def _extract_weather_args(sid: str, after: int) -> tuple[str | None, int]:
    """Read the get_weather tool-call args the agent used this turn.

    Returns (location, days). Parses only the session lines appended since the
    turn started so we pick up *this* turn's call, not a stale one.
    """
    location, days = None, 7
    try:
        path = _session_file(sid)
        cmd = f"tail -n +{after} {shlex.quote(path)} 2>/dev/null | head -c 400000"
        txt = _sbx_read(cmd)
    except Exception:  # noqa: BLE001
        return location, days
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:  # noqa: BLE001 — last line may be mid-write
            continue
        if o.get("type") != "message":
            continue
        for b in (o.get("message") or {}).get("content") or []:
            if not isinstance(b, dict) or b.get("type") != "toolCall":
                continue
            if "weather" not in (b.get("name") or ""):
                continue
            args = b.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:  # noqa: BLE001
                    args = {}
            if isinstance(args, dict):
                loc = args.get("location")
                if loc and str(loc).strip():
                    location = str(loc).strip()
                try:
                    days = int(args.get("days") or days)
                except (TypeError, ValueError):
                    pass
    return location, days


def _geocode(name: str) -> dict:
    r = requests.get(GEO_URL, params={"name": name, "count": 1, "language": "en",
                                      "format": "json"}, timeout=10)
    hit = ((r.json() or {}).get("results") or [None])[0]
    if not hit:
        raise ValueError(f"no place called {name!r}")
    label = ", ".join(x for x in (hit.get("name"), hit.get("admin1"),
                                  hit.get("country_code")) if x)
    return {"name": label, "latitude": hit["latitude"], "longitude": hit["longitude"]}


def build_weather_artifact(location: str | None, days: int = 7) -> dict | None:
    """Fetch a week of structured forecast data and shape it for the UI chart."""
    days = min(max(int(days or 7), 3), 7)
    try:
        if location:
            place = _geocode(location)
        else:
            name = _default_location_name()
            if not name:
                return None
            place = _geocode(name)
        r = requests.get(FORECAST_URL, params={
            "latitude": place["latitude"], "longitude": place["longitude"],
            "current": ("temperature_2m,apparent_temperature,relative_humidity_2m,"
                        "precipitation,weather_code,wind_speed_10m"),
            "daily": ("temperature_2m_max,temperature_2m_min,"
                      "precipitation_probability_max,weather_code,sunrise,sunset"),
            "hourly": "temperature_2m,weather_code,precipitation_probability",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "precipitation_unit": "inch", "timezone": "auto",
            "forecast_days": days,
        }, timeout=12)
        j = r.json() or {}
    except Exception:  # noqa: BLE001 — artifact is best-effort, never blocks a reply
        return None

    cur = j.get("current") or {}
    cdesc, cemoji = _wmo(cur.get("weather_code"))
    current = {
        "temp": round(cur.get("temperature_2m")) if cur.get("temperature_2m") is not None else None,
        "feels": round(cur.get("apparent_temperature")) if cur.get("apparent_temperature") is not None else None,
        "humidity": cur.get("relative_humidity_2m"),
        "wind": round(cur.get("wind_speed_10m")) if cur.get("wind_speed_10m") is not None else None,
        "code": cur.get("weather_code"), "desc": cdesc, "emoji": cemoji,
    }

    d = j.get("daily") or {}
    daily = []
    import datetime as _dt
    for i, day in enumerate(d.get("time") or []):
        desc, emoji = _wmo((d.get("weather_code") or [None])[i] if i < len(d.get("weather_code") or []) else None)
        try:
            wd = _dt.date.fromisoformat(day).strftime("%a")
        except Exception:  # noqa: BLE001
            wd = day
        daily.append({
            "date": day, "weekday": "Today" if i == 0 else wd,
            "tmax": round(d["temperature_2m_max"][i]) if d.get("temperature_2m_max") else None,
            "tmin": round(d["temperature_2m_min"][i]) if d.get("temperature_2m_min") else None,
            "precip": (d.get("precipitation_probability_max") or [None] * (i + 1))[i],
            "code": (d.get("weather_code") or [None] * (i + 1))[i],
            "desc": desc, "emoji": emoji,
        })

    h = j.get("hourly") or {}
    hourly = []
    times = h.get("time") or []
    temps = h.get("temperature_2m") or []
    hcodes = h.get("weather_code") or []
    # Start at the current hour; keep the next ~24 hours for the intraday sparkline.
    now_iso = (cur.get("time") or "")[:13]
    start = 0
    for idx, t in enumerate(times):
        if t[:13] >= now_iso:
            start = idx
            break
    for k in range(start, min(start + 24, len(times))):
        hourly.append({
            "time": times[k], "temp": round(temps[k]) if k < len(temps) and temps[k] is not None else None,
            "code": hcodes[k] if k < len(hcodes) else None,
        })

    place_name = place["name"]
    return {
        "type": "weather",
        "title": f"{place_name.split(',')[0]} — {len(daily)}-day forecast",
        "location": place_name,
        "units": {"temp": "°F", "wind": "mph"},
        "current": current,
        "daily": daily,
        "hourly": hourly,
    }


def build_turn_artifact(tools: list[str], sid: str, after: int) -> dict | None:
    """Return an artifact for a finished turn, or None.

    Currently supports weather. New artifact kinds slot in here by matching the
    tool name and building their own payload.
    """
    if any("get_weather" in (t or "") for t in tools):
        location, days = _extract_weather_args(sid, after)
        return build_weather_artifact(location, days)
    return None
