"""Thin wrapper around FlightAware's AeroAPI v4 for looking up a day's actual
flights by tail number — used as an alternative to photographing the FMS
display, for aircraft (e.g. the Cirrus) that don't have an ACARS/FMS TIMES
page to photograph but do fly with ADS-B Out and get tracked.

Free "Personal" tier, no credit card required: https://www.flightaware.com/aeroapi/portal
Docs: GET /flights/{ident} — https://www.flightaware.com/commercial/aeroapi/v4/

Field names verified 2026-07-13 against a live response for N522DX: the actual
takeoff/landing timestamps are "actual_off"/"actual_on" (ISO 8601 datetimes).
"actual_runway_off"/"actual_runway_on", despite the name, are NOT timestamps —
they're the runway identifier used (e.g. "23", "05") — an earlier doc summary
conflated the two field families; don't fall back to them for times.
"""
import os
from datetime import datetime, timedelta, timezone

import requests

API_KEY = os.environ.get('FLIGHTAWARE_API_KEY', '')
BASE_URL = 'https://aeroapi.flightaware.com/aeroapi'


def _first(d, *keys):
    for k in keys:
        if d.get(k):
            return d[k]
    return None


def _iso_to_hhmm_and_date(iso_str):
    if not iso_str:
        return None, None
    dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00')).astimezone(timezone.utc)
    return dt.strftime('%H%M'), dt.date().isoformat()


class FlightAwareError(Exception):
    pass


def flights_for_day(tail, flight_date):
    """Returns a list of completed flights (both actual off and on times
    present, not cancelled) for `tail` on `flight_date` (a date object),
    oldest first. Each item: {dep_airport, arr_airport, time_off, time_on,
    date, aircraft_type, fa_flight_id}."""
    if not API_KEY:
        raise FlightAwareError('FLIGHTAWARE_API_KEY is not set in .env')

    start = f'{flight_date.isoformat()}T00:00:00Z'
    end = f'{(flight_date + timedelta(days=1)).isoformat()}T00:00:00Z'
    try:
        resp = requests.get(
            f'{BASE_URL}/flights/{tail}',
            params={'ident_type': 'registration', 'start': start, 'end': end, 'max_pages': 1},
            headers={'x-apikey': API_KEY},
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        raise FlightAwareError(f'Could not reach FlightAware: {e}')

    if resp.status_code == 401:
        raise FlightAwareError('Invalid FlightAware API key')
    if resp.status_code == 404:
        return []
    if resp.status_code == 429:
        raise FlightAwareError('FlightAware rate limit hit — wait a moment and try again')
    if resp.status_code >= 500:
        raise FlightAwareError(f'FlightAware is temporarily unavailable ({resp.status_code}) — try again shortly')
    resp.raise_for_status()

    out = []
    for f in resp.json().get('flights', []):
        if f.get('cancelled'):
            continue
        off_iso = f.get('actual_off')
        on_iso = f.get('actual_on')
        if not off_iso or not on_iso:
            continue  # still in progress, or no actual times recorded
        time_off, date_off = _iso_to_hhmm_and_date(off_iso)
        time_on, _ = _iso_to_hhmm_and_date(on_iso)
        origin = f.get('origin') or {}
        destination = f.get('destination') or {}
        out.append({
            'fa_flight_id': f.get('fa_flight_id'),
            'date': date_off,
            'dep_airport': _first(origin, 'code_icao', 'code') or '',
            'arr_airport': _first(destination, 'code_icao', 'code') or '',
            'time_off': time_off,
            'time_on': time_on,
            'aircraft_type': f.get('aircraft_type', ''),
        })
    out.sort(key=lambda x: x['time_off'] or '')
    return out
