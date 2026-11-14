"""Time & location: site settings, NTP status from chrony, sunrise/sunset. Standard library only.

The CAP runs without root, so it can't change the reader's system clock, time zone or NTP
servers. Meridian keeps its own *site* time zone and uses it for display, rules and logs;
the reader's clock itself stays on UTC and is disciplined by chronyd.
"""
import datetime as dt
import math
import re
import subprocess

try:
    import zoneinfo
except ImportError:          # pragma: no cover
    zoneinfo = None

SITE_DEFAULTS = {"site_id": "", "site_name": "", "location": "", "timezone": "UTC",
                 "clock_24h": False, "latitude": None, "longitude": None}
SITE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
COMMON_ZONES = ["America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
                "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu", "America/Toronto",
                "America/Mexico_City", "Europe/London", "Europe/Berlin", "Asia/Shanghai", "UTC"]


# ---------------------------------------------------------------- time zones
def zone(name):
    """ZoneInfo for name, or None if unknown / no tz database."""
    if not zoneinfo or not name:
        return None
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return None


def available_zones():
    if not zoneinfo:
        return ["UTC"]
    try:
        names = {z for z in zoneinfo.available_timezones()
                 if "/" in z and not z.startswith(("Etc/", "SystemV/", "US/", "posix", "right"))}
    except OSError:
        names = set()
    names.add("UTC")
    return sorted(names)


def now_in(name):
    z = zone(name) or dt.timezone.utc
    return dt.datetime.now(dt.timezone.utc).astimezone(z)


# ---------------------------------------------------------------- site settings
def _coord(v, lo, hi, label, errors):
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        errors.append(f"{label} must be a number.")
        return None
    if not (lo <= f <= hi) or math.isnan(f):
        errors.append(f"{label} must be between {lo} and {hi}.")
        return None
    return round(f, 6)


def validate_site(body):
    """Return (clean_settings, errors)."""
    errors = []
    if not isinstance(body, dict):
        return None, ["Settings must be an object."]
    out = dict(SITE_DEFAULTS)
    sid = str(body.get("site_id") or "").strip().lower()
    if sid and not SITE_ID_RE.match(sid):
        errors.append("Site ID: lowercase letters, digits and dashes, up to 40 characters (it's used in MQTT topics).")
    out["site_id"] = sid
    for key, n in (("site_name", 80), ("location", 120)):
        val = str(body.get(key) or "").strip()
        if len(val) > n:
            errors.append(f"{key.replace('_', ' ').capitalize()} is limited to {n} characters.")
        out[key] = val[:n]
    tz = str(body.get("timezone") or "UTC").strip()
    if tz != "UTC" and zone(tz) is None:
        errors.append(f"Unknown time zone “{tz}”.")
    out["timezone"] = tz
    out["clock_24h"] = bool(body.get("clock_24h"))
    out["latitude"] = _coord(body.get("latitude"), -90, 90, "Latitude", errors)
    out["longitude"] = _coord(body.get("longitude"), -180, 180, "Longitude", errors)
    if (out["latitude"] is None) != (out["longitude"] is None) and not errors:
        errors.append("Enter both latitude and longitude, or neither.")
    return out, errors


# ---------------------------------------------------------------- NTP (chrony)
def _run(args):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=3)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


SOURCE_STATE = {"*": "in use", "+": "combined", "-": "not combined", "?": "unreachable",
                "x": "may be in error", "~": "too variable"}
SOURCE_MODE = {"^": "server", "=": "peer", "#": "local clock"}


def parse_sources(csv_text):
    rows = []
    for line in (csv_text or "").splitlines():
        p = line.split(",")
        if len(p) < 10:
            continue
        try:
            rows.append({"mode": SOURCE_MODE.get(p[0], p[0]), "state": SOURCE_STATE.get(p[1], p[1]),
                         "selected": p[1] == "*", "name": p[2], "stratum": int(p[3]),
                         "reach": int(p[5], 8), "last_rx_s": int(p[6]) if p[6].isdigit() else None,
                         "offset_ms": round(float(p[7]) * 1000, 3)})
        except ValueError:
            continue
    return rows


def parse_tracking(csv_text):
    p = (csv_text or "").strip().split(",")
    if len(p) < 14:
        return None
    try:
        return {"reference": p[1], "stratum": int(p[2]), "ref_time": float(p[3]),
                "offset_ms": round(float(p[4]) * 1000, 3), "update_interval_s": round(float(p[12]), 1),
                "leap": p[13], "synced": p[13] == "Normal"}
    except ValueError:
        return None


def ntp_status():
    tracking = parse_tracking(_run(["chronyc", "-c", "-n", "tracking"]))
    sources = parse_sources(_run(["chronyc", "-c", "-n", "sources"]))
    return {"available": tracking is not None, "tracking": tracking, "sources": sources}


# ---------------------------------------------------------------- sunrise / sunset (NOAA)
def _sun_utc_minutes(day, lat, lon, rising, zenith=90.833):
    """Minutes after 00:00 UTC of the event, or None (polar day/night). NOAA equations."""
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    la = math.radians(lat)
    cos_h = (math.cos(math.radians(zenith)) / (math.cos(la) * math.cos(decl))) - math.tan(la) * math.tan(decl)
    if cos_h < -1 or cos_h > 1:
        return None
    ha = math.degrees(math.acos(cos_h))
    return 720 - 4 * (lon + (ha if rising else -ha)) - eqtime


def sun_times(lat, lon, tzname, day=None):
    """{'date', 'sunrise', 'sunset', 'day_length'} in the site zone as 'HH:MM' strings (None if none)."""
    z = zone(tzname) or dt.timezone.utc
    day = day or dt.datetime.now(z).date()
    out = {"date": day.isoformat()}
    times = {}
    for key, rising in (("sunrise", True), ("sunset", False)):
        m = _sun_utc_minutes(day, lat, lon, rising)
        if m is None:
            times[key] = None
            out[key] = None
            continue
        t = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=m)
        local = t.astimezone(z)
        # a far-west/east longitude can land the UTC result on the neighbouring local day
        if local.date() != day:
            t += dt.timedelta(days=(day - local.date()).days)
            local = t.astimezone(z)
        times[key] = local
        out[key] = local.strftime("%H:%M")
    if times.get("sunrise") and times.get("sunset"):
        secs = int((times["sunset"] - times["sunrise"]).total_seconds())
        out["day_length"] = f"{secs // 3600}h {secs % 3600 // 60}m"
    else:
        out["day_length"] = None
    return out
