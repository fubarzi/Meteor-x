"""Current-weather lookup and image selection for the station header."""
from __future__ import annotations

import math
from dataclasses import dataclass
from urllib.parse import urlencode


API_URL = "https://api.open-meteo.com/v1/forecast"
REFRESH_MS = 15 * 60 * 1000

# Vertical crop position in each panoramic image. Moon and lightning need a
# higher crop than the daytime landscapes when shown in the shallow header.
CROP_TOP = {
    "sunny": 0.48,
    "partly_cloudy": 0.38,
    "overcast": 0.50,
    "fog": 0.48,
    "fog_night": 0.44,
    "drizzle": 0.48,
    "drizzle_night": 0.34,
    "rain": 0.40,
    "storm": 0.36,
    "storm_night": 0.48,
    "hail": 0.40,
    "hail_night": 0.42,
    "snow": 0.48,
    "snow_night": 0.11,
    "clear_night": 0.11,
    "cloudy_night": 0.12,
    "rain_night": 0.12,
}


@dataclass(frozen=True)
class CurrentWeather:
    code: int
    is_day: bool
    valid_time: str
    image: str


def image_for(code: int, is_day: bool) -> str | None:
    """Map Open-Meteo WMO codes onto the approved weather images."""
    if code in (0, 1):
        return "sunny" if is_day else "clear_night"
    if code == 2:
        return "partly_cloudy" if is_day else "cloudy_night"
    if code == 3:
        return "overcast" if is_day else "cloudy_night"
    if code in (45, 48):
        return "fog" if is_day else "fog_night"
    if code in (51, 53, 55, 56, 57):
        return "drizzle" if is_day else "drizzle_night"
    if code in (61, 63, 65, 66, 67, 80, 81, 82):
        return "rain" if is_day else "rain_night"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow" if is_day else "snow_night"
    if code in (95, 97):
        return "storm" if is_day else "storm_night"
    if code in (96, 99):
        return "hail" if is_day else "hail_night"
    return None


def request_url(latitude: float, longitude: float) -> str:
    if not (math.isfinite(latitude) and math.isfinite(longitude) and
            -90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Station latitude/longitude are invalid")
    query = urlencode({"latitude": f"{latitude:.6f}",
                       "longitude": f"{longitude:.6f}",
                       "current": "weather_code,is_day",
                       "timezone": "auto"})
    return f"{API_URL}?{query}"


def parse_current(payload: dict) -> CurrentWeather:
    current = payload["current"]
    code = int(current["weather_code"])
    day = int(current["is_day"])
    if day not in (0, 1):
        raise ValueError("Weather service returned an invalid day/night value")
    image = image_for(code, bool(day))
    if image is None:
        raise ValueError(f"Weather service returned unsupported code {code}")
    return CurrentWeather(code, bool(day), str(current.get("time", "")), image)
