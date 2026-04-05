"""Morning briefing data collection - replaces briefing_fixed.sh."""
import os
import json
import urllib.request
from datetime import datetime
from config import LATITUDE, LONGITUDE, WORKSPACE


def get_weather():
    """Get weather for Cayuga, ON from Open-Meteo."""
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={LATITUDE}&longitude={LONGITUDE}"
            f"&current=temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code"
            f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code"
            f"&timezone=America/New_York"
        )
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())

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
    """Get cistern level from PT Devices API."""
    try:
        url = "https://api.ptdevices.com/token/v1/device/4999?api_token=qrFjzQgLkmtcozOuOQrvh53u55Qo1JWvSwBJnxasREG5HSyMYRD3v4CRCMun"
        with urllib.request.urlopen(url, timeout=10) as r:
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
        # BTC from CoinGecko
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd,cad&include_24hr_change=true"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        btc = data["bitcoin"]
        btc_usd = btc["usd"]
        btc_cad = btc["cad"]
        change = btc.get("usd_24h_change", 0)
        arrow = "+" if change >= 0 else ""
        return f"BTC: ${btc_usd:,.0f} USD / ${btc_cad:,.0f} CAD ({arrow}{change:.1f}%)"
    except Exception as e:
        return f"Crypto unavailable: {e}"


TOMTOM_KEY = os.environ.get("TOMTOM_API_KEY", "")
HOME = (42.984746, -79.86937)
COMMUTES = {
    "Emily (Caledonia)": (43.070767, -79.953349),
    "Colin (Mohawk College)": (43.2082214, -79.7153766),
    "Paula (Hamilton)": (43.255574, -79.87182),
}


def get_traffic():
    """Get commute times and incidents from TomTom."""
    try:
        lines = []
        for name, (lat, lon) in COMMUTES.items():
            url = (
                f"https://api.tomtom.com/routing/1/calculateRoute/"
                f"{HOME[0]},{HOME[1]}:{lat},{lon}/json"
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

        return "\n".join(lines) if lines else "No routes available"
    except Exception as e:
        return f"Traffic unavailable: {e}"


def get_incidents():
    """Get traffic incidents near Cayuga from TomTom."""
    try:
        # Search in a bounding box around Cayuga/Hamilton corridor
        url = (
            f"https://api.tomtom.com/traffic/services/5/incidentDetails"
            f"?key={TOMTOM_KEY}"
            f"&bbox={HOME[1]-0.3},{HOME[0]-0.1},{HOME[1]+0.3},{HOME[0]+0.4}"
            f"&fields={{incidents{{type,geometry{{type,coordinates}},properties{{iconCategory,magnitudeOfDelay,events{{description}},from,to}}}}}}"
            f"&language=en-US&categoryFilter=0,1,2,3,4,5,6,7,8,9,10,11,14"
        )
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())

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
    cistern = get_cistern()
    crypto = get_crypto()
    grocery = get_grocery_status()
    traffic = get_traffic()
    incidents = get_incidents()
    return (
        f"Good morning! {now.strftime('%A, %B %d, %Y')}\n\n"
        f"CAYUGA WEATHER\n{weather}\n\n"
        f"CISTERN\n{cistern}\n\n"
        f"FAMILY COMMUTES\n{traffic}\n\n"
        f"ROAD INCIDENTS\n{incidents}\n\n"
        f"GROCERIES\n{grocery}\n\n"
        f"MARKETS\n{crypto}\n\n"
        f"{now.strftime('%H:%M')} EDT"
    )
