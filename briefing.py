"""Morning briefing data collection."""
import os
import json
import urllib.request
from datetime import datetime

import config


def get_weather():
    """Get weather from Open-Meteo."""
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={config.LATITUDE}&longitude={config.LONGITUDE}"
            f"&current=temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code"
            f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code"
            f"&timezone={config.TIMEZONE}"
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=15) as r:
                    data = json.loads(r.read())
                break
            except Exception:
                if attempt == 2:
                    raise
                import time
                time.sleep(2)

        current = data["current"]
        daily = data["daily"]

        codes = {
            0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
            45: "Foggy", 51: "Light drizzle", 61: "Light rain", 63: "Moderate rain",
            65: "Heavy rain", 71: "Light snow", 73: "Moderate snow", 75: "Heavy snow",
            80: "Rain showers", 95: "Thunderstorm",
        }

        tc = current["temperature_2m"]
        fc = current["apparent_temperature"]
        hc = daily["temperature_2m_max"][0]
        lc = daily["temperature_2m_min"][0]

        return (
            f"{codes.get(current['weather_code'], 'Unknown')} "
            f"{tc:.0f}C ({tc*9/5+32:.0f}F), feels {fc:.0f}C ({fc*9/5+32:.0f}F)\n"
            f"High {hc:.0f}C/{hc*9/5+32:.0f}F, Low {lc:.0f}C/{lc*9/5+32:.0f}F\n"
            f"Humidity {current['relative_humidity_2m']}%, Wind {current['wind_speed_10m']} km/h"
        )
    except Exception as e:
        return f"Weather unavailable: {e}"


def get_cistern():
    """Get cistern level from PT Devices API (optional)."""
    api_url = os.environ.get("CISTERN_API_URL", "")
    if not api_url:
        return None
    try:
        with urllib.request.urlopen(api_url, timeout=10) as r:
            d = json.loads(r.read())["data"]["device_data"]

        level = d["percent_level"]
        battery = d["battery_status"]
        temp = d["enclosure_temperature"]

        if level > 100:
            return f"~100% (sensor: {level}%) | Battery: {battery} | Temp: {temp}C"
        return f"{level}% | Battery: {battery} | Temp: {temp}C"
    except Exception as e:
        return f"Unavailable: {e}"


def get_crypto():
    """Get BTC and gold prices."""
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd,cad&include_24hr_change=true"
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=15) as r:
                    data = json.loads(r.read())
                break
            except Exception:
                if attempt == 2:
                    raise
                import time
                time.sleep(2)
        btc = data["bitcoin"]
        btc_usd = btc["usd"]
        btc_cad = btc["cad"]
        change = btc.get("usd_24h_change", 0)
        arrow = "+" if change >= 0 else ""
        return f"BTC: ${btc_usd:,.0f} USD / ${btc_cad:,.0f} CAD ({arrow}{change:.1f}%)"
    except Exception as e:
        return f"Crypto unavailable: {e}"


TOMTOM_KEY = os.environ.get("TOMTOM_API_KEY", "")


def _get_commutes():
    """Load commute destinations from config."""
    commutes = config._cfg.get("briefing", {}).get("commutes", {})
    return commutes


def get_traffic():
    """Get commute times from TomTom."""
    if not TOMTOM_KEY:
        return None
    commutes = _get_commutes()
    if not commutes:
        return None
    home = (config.LATITUDE, config.LONGITUDE)
    try:
        lines = []
        for name, dest in commutes.items():
            lat, lon = dest["lat"], dest["lon"]
            url = (
                f"https://api.tomtom.com/routing/1/calculateRoute/"
                f"{home[0]},{home[1]}:{lat},{lon}/json"
                f"?key={TOMTOM_KEY}&traffic=true&travelMode=car"
            )
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read())
            route = data["routes"][0]["summary"]
            mins = route["travelTimeInSeconds"] // 60
            delay = route.get("trafficDelayInSeconds", 0) // 60
            km = route["lengthInMeters"] / 1000
            line = f"  {name}: {mins} min ({km:.0f} km)"
            if delay > 2:
                line += f" +{delay} min delay"
            lines.append(line)

        return "\n".join(lines) if lines else None
    except Exception as e:
        return f"Traffic unavailable: {e}"


def get_incidents():
    """Get traffic incidents near home from TomTom."""
    if not TOMTOM_KEY:
        return None
    home = (config.LATITUDE, config.LONGITUDE)
    try:
        url = (
            f"https://api.tomtom.com/traffic/services/5/incidentDetails"
            f"?key={TOMTOM_KEY}"
            f"&bbox={home[1]-0.3},{home[0]-0.1},{home[1]+0.3},{home[0]+0.4}"
            f"&fields={{incidents{{type,geometry{{type,coordinates}},properties{{iconCategory,magnitudeOfDelay,events{{description}},from,to}}}}}}"
            f"&language=en-US&categoryFilter=0,1,2,3,4,5,6,7,8,9,10,11,14"
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=15) as r:
                    data = json.loads(r.read())
                break
            except Exception:
                if attempt == 2:
                    raise
                import time
                time.sleep(2)

        incidents = data.get("incidents", [])
        if not incidents:
            return "No incidents"

        lines = []
        for inc in incidents[:5]:
            props = inc.get("properties", {})
            events = props.get("events", [])
            desc = events[0].get("description", "Incident") if events else "Incident"
            road_from = props.get("from", "")
            road_to = props.get("to", "")
            road = f"{road_from} to {road_to}" if road_from else "Unknown road"
            lines.append(f"  {desc}: {road}")

        return "\n".join(lines)
    except Exception as e:
        return f"Incidents unavailable: {e}"


def get_grocery_status():
    """Quick grocery status from inventory DB."""
    import db
    low = db.get_low_stock_items()
    shopping = db.get_shopping_list()

    parts = []
    if low:
        parts.append(f"{len(low)} items out of stock")
    if shopping:
        parts.append(f"{len(shopping)} items on shopping list")
    if not parts:
        return "All stocked up!"
    return ", ".join(parts)


def get_email_status():
    """Quick unread email count."""
    if not config.EMAIL_ENABLED:
        return None
    try:
        import email_client
        count = email_client.get_unread_count()
        if count == 0:
            return "Inbox clear"
        return f"{count} unread"
    except Exception:
        return "Unavailable"


def build_briefing():
    """Build the full morning briefing message."""
    now = datetime.now()
    weather = get_weather()
    grocery = get_grocery_status()
    crypto = get_crypto()

    sections = [
        f"Good morning! {now.strftime('%A, %B %d, %Y')}",
        f"\nWEATHER\n{weather}",
    ]

    cistern = get_cistern()
    if cistern:
        sections.append(f"\nCISTERN\n{cistern}")

    traffic = get_traffic()
    if traffic:
        sections.append(f"\nCOMMUTES\n{traffic}")

    incidents = get_incidents()
    if incidents:
        sections.append(f"\nROAD INCIDENTS\n{incidents}")

    sections.append(f"\nGROCERIES\n{grocery}")

    email_status = get_email_status()
    if email_status:
        sections.append(f"\nEMAIL\n{email_status}")

    sections.append(f"\nMARKETS\n{crypto}")
    sections.append(f"\n{now.strftime('%H:%M')} {config.TIMEZONE}")

    return "\n".join(sections)
