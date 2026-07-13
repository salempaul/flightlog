import csv, math, os, re
from datetime import datetime, date, time, timedelta, timezone
from astral import Observer
from astral.sun import elevation

DATA_PATH  = os.path.join(os.path.dirname(__file__), 'data', 'airports.csv')
GRID_SIZE  = 1.0      # degrees per spatial-index bucket
NIGHT_ELEV = -6.0      # civil twilight threshold (FAA "night" definition, 14 CFR 1.1)
OCEAN_NM   = 50         # extended-overwater threshold, 14 CFR 135.183
EARTH_NM   = 3440.065

_airports  = []   # list of dicts: ident, iata, type, lat, lon, country, name
_grid      = {}   # (lat_bucket, lon_bucket) -> [index,...]
_by_ident  = {}   # 4-char ICAO ident -> record (unambiguous)
_by_iata   = {}   # 3-char IATA code -> [records,...] (can collide worldwide)

MONTHS = {m: i for i, m in enumerate(
    ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'], start=1)}

_TYPE_RANK = {'large_airport': 3, 'medium_airport': 2, 'small_airport': 1}


def _bucket(lat, lon):
    return (math.floor(lat / GRID_SIZE), math.floor(lon / GRID_SIZE))


def _load():
    if _airports:
        return
    with open(DATA_PATH, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            try:
                lat = float(row['lat']); lon = float(row['lon'])
            except (ValueError, TypeError):
                continue
            rec = {'ident': row['ident'], 'iata': row['iata'], 'type': row['type'],
                   'lat': lat, 'lon': lon, 'country': row['country'], 'name': row['name']}
            idx = len(_airports)
            _airports.append(rec)
            _grid.setdefault(_bucket(lat, lon), []).append(idx)
            if rec['ident']:
                _by_ident[rec['ident'].upper()] = rec
            if rec['iata']:
                _by_iata.setdefault(rec['iata'].upper(), []).append(rec)


def find_airport(code):
    """Look up an airport by ICAO or IATA/FAA code (case-insensitive).

    PFM (and most US scheduling systems) print 3-letter codes that are
    usually a truncated US ICAO ident (K-prefix dropped, e.g. "CRQ" for
    KCRQ) rather than a true IATA code — those 3-letter strings collide
    with unrelated IATA codes elsewhere in the world (e.g. "CRQ" is also
    Caravelas, Brazil). So: try exact ICAO, then the US K-prefix guess,
    before falling back to a worldwide IATA match.
    """
    _load()
    if not code:
        return None
    c = code.strip().upper()
    if len(c) == 4 and c in _by_ident:
        return _by_ident[c]
    if len(c) == 3:
        us_icao = 'K' + c
        if us_icao in _by_ident:
            return _by_ident[us_icao]
    if c in _by_ident:
        return _by_ident[c]
    candidates = _by_iata.get(c)
    if candidates:
        candidates = sorted(candidates, key=lambda r: (r['country'] != 'US', -_TYPE_RANK.get(r['type'], 0)))
        return candidates[0]
    return None


def haversine_nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_NM * math.asin(math.sqrt(a))


def nearest_airport_distance_nm(lat, lon):
    """Approximate distance to the nearest airport in the DB, used as a proxy for
    distance-from-land (coastal/island airstrips dot every populated coastline)."""
    _load()
    bl = _bucket(lat, lon)
    best = None
    radius = 1
    while radius <= 8:
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                for idx in _grid.get((bl[0] + dx, bl[1] + dy), []):
                    rec = _airports[idx]
                    d = haversine_nm(lat, lon, rec['lat'], rec['lon'])
                    if best is None or d < best:
                        best = d
        # once we have a candidate closer than the guaranteed-scanned radius, stop
        if best is not None and best < (radius * GRID_SIZE * 60 * 0.8):
            break
        radius += 1
    return best


# ── Great-circle interpolation ─────────────────────────────────────────────

def _to_xyz(lat, lon):
    lat, lon = math.radians(lat), math.radians(lon)
    return (math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat))


def _to_latlon(x, y, z):
    return math.degrees(math.asin(max(-1.0, min(1.0, z)))), math.degrees(math.atan2(y, x))


def interpolate_great_circle(lat1, lon1, lat2, lon2, frac):
    if frac <= 0: return lat1, lon1
    if frac >= 1: return lat2, lon2
    x1, y1, z1 = _to_xyz(lat1, lon1)
    x2, y2, z2 = _to_xyz(lat2, lon2)
    dot = max(-1.0, min(1.0, x1 * x2 + y1 * y2 + z1 * z2))
    theta = math.acos(dot)
    if theta < 1e-9:
        return lat1, lon1
    sin_t = math.sin(theta)
    a = math.sin((1 - frac) * theta) / sin_t
    b = math.sin(frac * theta) / sin_t
    return _to_latlon(a * x1 + b * x2, a * y1 + b * y2, a * z1 + b * z2)


def route_distance_nm(dep_rec, arr_rec):
    return haversine_nm(dep_rec['lat'], dep_rec['lon'], arr_rec['lat'], arr_rec['lon'])


# ── Date parsing (trip.date "10 JUL 26" + leg.date_z "10JUL") ──────────────

def parse_leg_date(trip_date_str, leg_date_z):
    year = None
    m = re.search(r'(\d{2,4})\s*$', trip_date_str or '')
    if m:
        y = int(m.group(1))
        year = y + 2000 if y < 100 else y

    for src in (leg_date_z, trip_date_str):
        m2 = re.match(r'\s*(\d{1,2})\s*([A-Za-z]{3})', src or '')
        if m2:
            mon = MONTHS.get(m2.group(2).upper())
            if mon:
                return date(year or datetime.now(timezone.utc).year, mon, int(m2.group(1)))
    return None


def _hhmm_to_minutes(hhmm):
    try:
        s = str(hhmm).zfill(4)
        return int(s[:2]) * 60 + int(s[2:])
    except Exception:
        return None


# ── Night time / night landings (civil twilight, sampled along the route) ──

def compute_night(dep_rec, arr_rec, flight_date, time_off, time_on, samples=20):
    """Returns (night_minutes: float, night_landing: bool)."""
    if not (dep_rec and arr_rec and flight_date and time_off and time_on):
        return 0.0, False

    off_min = _hhmm_to_minutes(time_off)
    on_min = _hhmm_to_minutes(time_on)
    if off_min is None or on_min is None:
        return 0.0, False
    total_min = on_min - off_min
    if total_min < 0:
        total_min += 1440
    if total_min <= 0:
        return 0.0, False

    start_dt = datetime.combine(flight_date, time(off_min // 60, off_min % 60), tzinfo=timezone.utc)
    n = max(samples, 2)
    step = total_min / n
    night_minutes = 0.0
    for i in range(n):
        t_mid = (i + 0.5) * step
        dt = start_dt + timedelta(minutes=t_mid)
        frac = t_mid / total_min
        lat, lon = interpolate_great_circle(dep_rec['lat'], dep_rec['lon'],
                                             arr_rec['lat'], arr_rec['lon'], frac)
        if elevation(Observer(latitude=lat, longitude=lon), dt) < NIGHT_ELEV:
            night_minutes += step

    end_dt = start_dt + timedelta(minutes=total_min)
    night_landing = elevation(Observer(latitude=arr_rec['lat'], longitude=arr_rec['lon']), end_dt) < NIGHT_ELEV
    return night_minutes, night_landing


# ── International / ocean crossing ──────────────────────────────────────────

def is_international(dep_rec, arr_rec):
    if not (dep_rec and arr_rec):
        return False
    return dep_rec['country'] != 'US' or arr_rec['country'] != 'US'


def is_ocean_crossing(dep_rec, arr_rec, samples=40):
    """Flags a leg if any point along the great-circle route is >50nm from the
    nearest airport in the database — a proxy for 'extended overwater' since
    small strips/heliports dot essentially every populated coastline."""
    if not (dep_rec and arr_rec):
        return False
    for i in range(1, samples):
        frac = i / samples
        lat, lon = interpolate_great_circle(dep_rec['lat'], dep_rec['lon'],
                                             arr_rec['lat'], arr_rec['lon'], frac)
        d = nearest_airport_distance_nm(lat, lon)
        if d is not None and d > OCEAN_NM:
            return True
    return False
