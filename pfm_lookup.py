import json, os, sqlite3
from datetime import datetime

PFM_DB_PATH = os.environ.get('PFM_DB_PATH', '/home/paul/ScheduleMate/pfm.db')


def _sched_minutes(d):
    """Minutes-past-midnight-Z of the scheduled departure, or None."""
    st = (d.get('start_time') or {}).get('__dt__', '')
    try:
        dt = datetime.fromisoformat(st)
        return dt.hour * 60 + dt.minute
    except Exception:
        return None


def find_scheduled_leg(tail, flight_date, time_off_hhmm=None):
    """Look up PFM's schedule for a tail on a given date, to *suggest* the
    dep/arr airports for a flightlog entry. This is only ever a suggestion:
    the FMS photo's actual times/distances are the source of truth, and the
    caller is expected to sanity-check the suggested route (e.g. via
    geo.route_distance_nm vs the FMS-reported ground distance) before
    trusting it — a diversion means the scheduled route doesn't reflect
    what was actually flown.
    """
    if not tail or not flight_date or not os.path.exists(PFM_DB_PATH):
        return None

    date_str = flight_date.isoformat()
    conn = sqlite3.connect(PFM_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT data FROM events WHERE data LIKE ? AND data LIKE ?",
            (f'%"aircraft": "{tail}"%', f'%{date_str}%')
        ).fetchall()
    finally:
        conn.close()

    candidates = []
    for (raw,) in rows:
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if d.get('aircraft') != tail or d.get('is_status'):
            continue
        st = (d.get('start_time') or {}).get('__dt__', '')
        if not st.startswith(date_str):
            continue
        candidates.append(d)

    if not candidates:
        return None

    if time_off_hhmm and len(candidates) > 1:
        s = str(time_off_hhmm).zfill(4)
        target = int(s[:2]) * 60 + int(s[2:])
        candidates.sort(key=lambda d: abs((_sched_minutes(d) or 0) - target)
                         if _sched_minutes(d) is not None else 10**6)

    d = candidates[0]
    return {
        'tail': d.get('aircraft', tail),
        'dep_airport': d.get('dep_airport', ''),
        'arr_airport': d.get('arr_airport', ''),
        'crew': d.get('crew_str', ''),
    }


def find_scheduled_leg_by_date(flight_date, time_off_hhmm=None):
    """Like find_scheduled_leg, but for when the tail isn't known yet either
    (the FMS photo doesn't show one) — looks up *any* scheduled leg on the
    given date. Safe to do without a tail filter because this PFM database is
    Paul's own personal schedule sync, not a shared/multi-pilot one; every row
    is already implicitly "my" flight. Still only ever a suggestion — same
    caveat as find_scheduled_leg."""
    if not flight_date or not os.path.exists(PFM_DB_PATH):
        return None

    date_str = flight_date.isoformat()
    conn = sqlite3.connect(PFM_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT data FROM events WHERE data LIKE ?", (f'%{date_str}%',)
        ).fetchall()
    finally:
        conn.close()

    candidates = []
    for (raw,) in rows:
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if d.get('is_status') or not d.get('aircraft'):
            continue
        st = (d.get('start_time') or {}).get('__dt__', '')
        if not st.startswith(date_str):
            continue
        candidates.append(d)

    if not candidates:
        return None

    if time_off_hhmm and len(candidates) > 1:
        s = str(time_off_hhmm).zfill(4)
        target = int(s[:2]) * 60 + int(s[2:])
        candidates.sort(key=lambda d: abs((_sched_minutes(d) or 0) - target)
                         if _sched_minutes(d) is not None else 10**6)

    d = candidates[0]
    return {
        'tail': d.get('aircraft', ''),
        'dep_airport': d.get('dep_airport', ''),
        'arr_airport': d.get('arr_airport', ''),
        'crew': d.get('crew_str', ''),
    }
