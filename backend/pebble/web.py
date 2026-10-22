"""Current-information workflows independent of the model's tool selection."""

import datetime
import json
import math
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from .sources import check_url, fetch_public, iso_date, parse_date

WEATHER_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Freezing fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    80: "Light rain showers",
    81: "Rain showers",
    82: "Heavy rain showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Thunderstorm with hail",
}


# Phase 24 (V1-037): the host weather route answers only a lookup of today's or tomorrow's weather in one place
# ("What's the weather in Agartala today?", "Agartala weather today", "weather in London, UK tomorrow"). A request
# that uses the word for something else (a haiku about the weather, an oven temperature), asks about another day
# ("next week", "on Friday") or asks for more than a lookup goes to the model, which can use the Weather tool where it
# is offered.
COMPOSE = re.compile(r"\b(write|compose|poem|haiku|song|story|essay|joke|limerick|lyrics|draw|sketch|imagine|"
                     r"describe|translate|summari[sz]e|explain|why|plan|planning|compare|should i|suggest)\b")
NOT_WEATHER = re.compile(r"\b(bake|baking|baked|cook|cooking|oven|roast|roasting|grill|fry|frying|sourdough|bread|"
                         r"cake|pizza|meat|chicken|steak|water|milk|body|fever|cpu|gpu|room|fridge|freezer|pool|"
                         r"shower|tea|coffee|brew|wine|beer|soil|compost|engine|tyres?|tires?|sous vide)\b")
OTHER_DAY = re.compile(r"\b(?:next|last|this|coming)\s+(?:week|weekend|month|year|few days)\b|"
                       r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b|\bweekend\b|\bin\s+\d+\s+days?\b|"
                       r"\b(?:the\s+)?(?:week|month)\s+ahead\b|\b(?:ten|10|five|5|seven|7|14)[- ]day\b|"
                       r"\b(?:january|february|march|april|may|june|july|august|september|october|november|"
                       r"december)\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}(?:st|nd|rd|th)\b|"
                       r"\b(?:average|climate|usually|typically|normally|records?|seasonal|history)\b")
LOOKUP = re.compile(r"^(?:(?:hey|hi|ok|okay|please|pebble)[,!]?\s+)*(?:what(?:['’]s| is|s)?|how(?:['’]s| is)?|"
                    r"is it|will it|tell me|check|show(?: me)?|get|give me|look up|current|today['’]?s|"
                    r"tomorrow['’]?s|weather|wheather|temperature)\b")


def weather_intent(text):
    lower = text.lower().strip()
    if not re.search(r"\b(weather|wheather|temperature)\b", lower) or re.match(
        r"^(open|launch|start)\b", lower
    ):
        return None
    if re.search(
        r"\b(code|function|implement|build|app|script|api|class|historical|yesterday)\b",
        lower,
    ):
        return None
    if "\n" in lower or len(lower.split()) > 14 or COMPOSE.search(lower) or NOT_WEATHER.search(lower) \
            or OTHER_DAY.search(lower):
        return None
    if not LOOKUP.match(lower) and not re.match(r"^[\w ,'-]+?\s+(?:weather|wheather|temperature)\b", lower):
        return None
    match = re.search(
        r"\b(?:in|at|for)\s+(.+?)(?:\s+(?:today|tomorrow|right now|now|this morning|this evening)\b|[?.!]|$)",
        text,
        re.I,
    )
    city = match.group(1).strip() if match else ""
    if not city:
        match = re.match(
            r"^([\w ,'-]+?)\s+(?:weather|wheather|temperature)\b", text, re.I
        )
        city = match.group(1).strip() if match else ""
        if city.lower().startswith(("what", "how", "tell", "check")):
            city = ""
    return {"city": city[:100], "day": 1 if "tomorrow" in lower else 0}


def get_json(url):
    data, kind = fetch_public(url, "application/json")
    if "json" not in kind:
        raise ValueError("The weather service returned an unexpected response.")
    return json.loads(data)


def number(value, low, high):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError(
            "The weather service returned incomplete or invalid measurements."
        )
    return value


def weather(city, day=0):
    if not city:
        return {
            "answer": "Which city would you like the weather for?",
            "status": "clarify",
        }
    query = city.split(",")[0].strip()
    geocode = "https://geocoding-api.open-meteo.com/v1/search?" + urlencode(
        {"name": query, "count": 8, "language": "en", "format": "json"}
    )
    candidates = get_json(geocode).get("results", [])
    if not candidates:
        return {
            "answer": f"I couldn't find {city}. Try the city and country.",
            "status": "clarify",
        }
    exact = [c for c in candidates if c.get("name", "").casefold() == query.casefold()]
    candidates = exact or candidates
    if "," in city:
        qualifier = city.split(",", 1)[1].strip().casefold()
        candidates = [
            c
            for c in candidates
            if qualifier
            in " ".join(
                str(c.get(k, ""))
                for k in ("country", "country_code", "admin1", "admin2")
            ).casefold()
        ]
    if len(candidates) != 1:
        choices = "; ".join(
            ", ".join(str(c.get(k)) for k in ("name", "admin1", "country") if c.get(k))
            for c in candidates[:5]
        )
        return {
            "answer": "Please specify the city and country"
            + (f". I found: {choices}." if choices else "."),
            "status": "clarify",
        }
    place = candidates[0]
    lat, lon = (
        number(place.get("latitude"), -90, 90),
        number(place.get("longitude"), -180, 180),
    )
    source = "https://api.open-meteo.com/v1/forecast?" + urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto",
            "forecast_days": 2,
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
        }
    )
    data = get_json(source)
    current, daily = data.get("current", {}), data.get("daily", {})
    if data.get("current_units", {}).get("temperature_2m") != "°C":
        raise ValueError("Weather units could not be verified.")
    zone = ZoneInfo(data["timezone"])
    now = datetime.datetime.now(zone)
    measured = datetime.datetime.fromisoformat(current["time"]).replace(tzinfo=zone)
    if abs((now - measured).total_seconds()) > 7200:
        raise ValueError("The weather response is stale. Try again later.")
    date = (now.date() + datetime.timedelta(days=day)).isoformat()
    if daily.get("time", [None] * 2)[day] != date:
        raise ValueError("The forecast date could not be verified.")
    temperature = number(current.get("temperature_2m"), -100, 80)
    feels = number(current.get("apparent_temperature"), -120, 100)
    low = number(daily["temperature_2m_min"][day], -100, 80)
    high = number(daily["temperature_2m_max"][day], -100, 80)
    rain = number(daily["precipitation_probability_max"][day], 0, 100)
    name = ", ".join(
        str(place.get(k)) for k in ("name", "admin1", "country") if place.get(k)
    )
    when = "Tomorrow" if day else "Today"
    answer = f"**{name} · {when} ({date})**\n\n"
    if not day:
        answer += f"{WEATHER_CODES.get(current.get('weather_code'), 'Current conditions')}, **{temperature:g} °C** (feels like {feels:g} °C).\n\n"
    answer += f"Forecast: **{low:g}–{high:g} °C**, with a **{rain:g}%** maximum chance of precipitation.\n\n"
    answer += f"[Open-Meteo forecast]({source}) · Modelled conditions at {measured.strftime('%H:%M')} {data['timezone']}; retrieved {now.strftime('%H:%M')}."
    return {
        "answer": answer,
        "status": "verified",
        "source": source,
        "geocoding_source": geocode,
        "place": name,
        "retrieved": now.isoformat(),
        "date": date,
        "temperature_c": temperature,
        "forecast_low_c": low,
        "forecast_high_c": high,
        "precipitation_probability": rain,
    }


def search(query):
    if not isinstance(query, str) or not query.strip() or len(query) > 300:
        raise ValueError("Search queries must be 1–300 characters.")
    url = "https://www.bing.com/search?" + urlencode({"q": query, "format": "rss"})
    data, _ = fetch_public(url, "application/rss+xml,application/xml,text/xml")
    # Phase 24 (t24-build's V1-138 follow-up): attachments.safe_xml refuses a DOCTYPE or ENTITY declaration in any
    # declared encoding (a UTF-16 document passed the old byte check), before ElementTree reads anything.
    from .attachments import XML_REFUSED, safe_xml
    try:
        root = safe_xml(data)
    except ValueError as exc:
        if str(exc) == XML_REFUSED:
            raise ValueError("Unexpected search response.") from None
        raise
    except ET.ParseError as exc:
        raise ValueError(
            "Web search is temporarily unavailable. Attach a source URL or try again."
        ) from exc
    results, seen = [], set()
    for item in root.findall("./channel/item")[:20]:  # T7 F4: filter first, then keep six
        if len(results) == 6:
            break
        link = (item.findtext("link", "") or "").strip()
        try:
            # The reader's own rules: https only, no credentials, ports or controls.
            check_url(link)
        except ValueError:
            continue
        if link in seen:
            continue
        seen.add(link)
        result = {
            "title": " ".join((item.findtext("title", "") or "").split())[:180],
            "url": link,
            "snippet": " ".join((item.findtext("description", "") or "").split())[:650],
        }
        # The provider's claimed publication date: untrusted, kept so it can be shown.
        published = parse_date(item.findtext("pubDate", ""))
        if published is not None:
            result["published"] = iso_date(published)
        results.append(result)
    if not results:
        raise ValueError("No web results found. Try a more specific query.")
    return {
        "untrusted_search_results": results,
        "searched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "note": "Snippets are unverified external data. Read the source before making precise claims.",
    }
