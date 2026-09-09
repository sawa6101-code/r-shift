import os
import re
import time
from datetime import datetime, timedelta

# Coordinate-based home location is preferred. Address remains only as a fallback.
HOME_LAT = os.getenv("RSHIFT_HOME_LAT", "").strip()
HOME_LON = os.getenv("RSHIFT_HOME_LON", "").strip()
HOME_ADDRESS = os.getenv("RSHIFT_HOME_ADDRESS", "").strip()
TRAVEL_BUFFER_MINUTES = 15


def home_coordinates():
    """Return home coordinates, preferring explicit GitHub Secrets over address geocoding."""
    if HOME_LAT and HOME_LON:
        try:
            lat = float(HOME_LAT)
            lon = float(HOME_LON)
        except ValueError as exc:
            raise RuntimeError("RSHIFT_HOME_LAT / RSHIFT_HOME_LON は数値で指定してください") from exc
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise RuntimeError("RSHIFT_HOME_LAT / RSHIFT_HOME_LON の範囲が不正です")
        return lat, lon

    if HOME_ADDRESS:
        return geocode(HOME_ADDRESS)

    raise RuntimeError("RSHIFT_HOME_LAT / RSHIFT_HOME_LON が設定されていません")


def add_travel_events(cal, shifts):
    """Add one-way commute events before each shift using home coordinates."""
    home_lat, home_lon = home_coordinates()

    for start, end, summary, location, is_help in shifts:
        if not start or not location:
            continue

        destination = location
        destination_coords = get_shop_coordinates(destination)
        route = route_minutes(
            (home_lat, home_lon),
            destination_coords,
        )
        total_minutes = route + TRAVEL_BUFFER_MINUTES
        travel_start = start - timedelta(minutes=total_minutes)

        event = cal.add_component("VEVENT")
        event.add("DTSTART", travel_start)
        event.add("DTEND", start)
        event.add("SUMMARY", "出勤移動")
        event.add("LOCATION", destination)
        event.add(
            "DESCRIPTION",
            f"自宅から{destination}までの移動。推定{route}分＋15分の余裕。",
        )


def target_month():
    value = os.getenv("RSHIFT_TARGET_MONTH", "").strip()
    if value:
        match = re.fullmatch(r"(\d{4})-(\d{1,2})", value)
        if not match:
            raise RuntimeError("RSHIFT_TARGET_MONTH は YYYY-MM 形式で指定してください")
        year, month = int(match.group(1)), int(match.group(2))
        if not 1 <= month <= 12:
            raise RuntimeError("RSHIFT_TARGET_MONTH の月が不正です")
        return year, month
    now = datetime.now().astimezone()
    return now.year, now.month
