import os, csv, io, re, json, base64, urllib.request, subprocess
from flask import Flask, request, jsonify, render_template, send_file
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date, time, timedelta, timezone
from PIL import Image
import pytesseract
import numpy as np
import anthropic

import geo
import pfm_lookup
import flightaware

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///flightlog.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)


class Flight(db.Model):
    """One row per submitted flight (one photo in, one MyFlightbook entry out —
    no more trip/multi-leg grouping)."""
    id             = db.Column(db.Integer, primary_key=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    date           = db.Column(db.String(10), default='')   # YYYY-MM-DD
    tail           = db.Column(db.String(10), default='')
    dep_airport    = db.Column(db.String(4),  default='')
    arr_airport    = db.Column(db.String(4),  default='')
    time_off       = db.Column(db.String(4),  default='')   # HHMM zulu
    time_on        = db.Column(db.String(4),  default='')   # HHMM zulu
    role           = db.Column(db.String(3),  default='PIC')  # PIC or SIC
    taxi           = db.Column(db.Float,      default=0.3)
    instrument     = db.Column(db.Float,      default=0)
    landings       = db.Column(db.Integer,    default=1)
    cfi            = db.Column(db.Float,      default=0)     # instructor time given
    solo           = db.Column(db.Float,      default=0)
    holds          = db.Column(db.Integer,    default=0)     # count of hold entries
    approaches_ils = db.Column(db.Integer,    default=0)
    approaches_gps = db.Column(db.Integer,    default=0)     # RNAV/GPS, unspecified sub-type
    night_hours    = db.Column(db.Float,      default=0)     # computed, overridable
    night_landings = db.Column(db.Integer,    default=0)     # computed, overridable
    cross_country  = db.Column(db.Float,      default=0)     # computed, overridable
    international  = db.Column(db.Boolean,    default=False) # computed, overridable
    ocean_crossing = db.Column(db.Boolean,    default=False) # computed, overridable
    comments       = db.Column(db.Text,       default='')
    air_dist       = db.Column(db.Float)      # nm, from FMS OCR (reference/cross-check only)
    gnd_dist       = db.Column(db.Float)      # nm, from FMS OCR
    ocr_source     = db.Column(db.String(40), default='')
    mfb_flight_id  = db.Column(db.Integer)
    mfb_status     = db.Column(db.String(20), default='')    # created / duplicate / error
    mfb_message    = db.Column(db.Text,       default='')

with app.app_context():
    db.create_all()
    # db.create_all() only creates missing tables, not missing columns on an
    # existing one — this project has no Alembic/migration framework, so patch
    # new columns onto the real (already-populated) sqlite table by hand.
    from sqlalchemy import inspect, text
    existing_cols = {c['name'] for c in inspect(db.engine).get_columns('flight')}
    new_cols = {
        'cfi': 'FLOAT DEFAULT 0', 'solo': 'FLOAT DEFAULT 0',
        'holds': 'INTEGER DEFAULT 0',
        'approaches_ils': 'INTEGER DEFAULT 0', 'approaches_gps': 'INTEGER DEFAULT 0',
    }
    with db.engine.connect() as conn:
        for col, ddl in new_cols.items():
            if col not in existing_cols:
                conn.execute(text(f'ALTER TABLE flight ADD COLUMN {col} {ddl}'))
        conn.commit()

# ── Math helpers ──────────────────────────────────────────────────────────────

def hhmm_to_minutes(hhmm):
    try:
        s = str(hhmm).zfill(4)
        return int(s[:2]) * 60 + int(s[2:])
    except Exception:
        return None

def calc_flight_time(time_off, time_on):
    """Returns (total_minutes, 'H:MM' display) or (None, '') if either time is missing/invalid."""
    off, on = hhmm_to_minutes(time_off), hhmm_to_minutes(time_on)
    if off is None or on is None:
        return None, ''
    total = on - off
    if total < 0:
        total += 1440  # crossed midnight
    return total, f'{total // 60}:{total % 60:02d}'

def minutes_to_tenth(total_minutes):
    if total_minutes is None:
        return None
    return round(total_minutes / 60.0, 1)

# ── Auto-compute (night / international / ocean crossing / cross-country) ─────
# Always computed *suggestions* — the confirm table renders them editable, and
# whatever value is present in the final POST /api/flights body is what's saved.

XC_THRESHOLD_NM = 50  # 14 CFR 61.1(b)(3)(ii) — landing >50nm straight-line from departure

def autocalc(dep_code, arr_code, flight_date_str, time_off, time_on, taxi=0.3):
    dep = geo.find_airport(dep_code)
    arr = geo.find_airport(arr_code)
    flight_date = None
    if flight_date_str:
        try:
            y, m, d = (int(x) for x in flight_date_str.split('-'))
            flight_date = date(y, m, d)
        except Exception:
            flight_date = None

    total_min, display = calc_flight_time(time_off, time_on)
    flight_hr_tenth = minutes_to_tenth(total_min) or 0.0

    night_min, night_landing = (0.0, False)
    if dep and arr and flight_date and time_off and time_on:
        night_min, night_landing = geo.compute_night(dep, arr, flight_date, time_off, time_on)

    is_xc = bool(dep and arr and geo.route_distance_nm(dep, arr) >= XC_THRESHOLD_NM)

    return {
        'total_flight_time':  flight_hr_tenth,
        'flight_time_display': display,
        'total_loggable_hours': round(flight_hr_tenth + (taxi or 0), 1),
        'night_hours':         minutes_to_tenth(round(night_min)) or 0.0,
        'night_landings':      1 if night_landing else 0,
        'cross_country':       round(flight_hr_tenth + (taxi or 0), 1) if is_xc else 0.0,
        'international':       bool(dep and arr and geo.is_international(dep, arr)),
        'ocean_crossing':      bool(dep and arr and geo.is_ocean_crossing(dep, arr)),
        'route_distance_nm':   round(geo.route_distance_nm(dep, arr), 1) if dep and arr else None,
    }

# ── Serializer ────────────────────────────────────────────────────────────────

def flight_to_dict(f):
    total_min, display = calc_flight_time(f.time_off, f.time_on)
    flight_hr_tenth = minutes_to_tenth(total_min)
    taxi = f.taxi or 0
    total_loggable = round((flight_hr_tenth or 0) + taxi, 1) if flight_hr_tenth is not None else None
    return {
        'id':                   f.id,
        'created_at':           f.created_at.isoformat() if f.created_at else None,
        'date':                 f.date,
        'tail':                 f.tail,
        'dep_airport':          f.dep_airport,
        'arr_airport':          f.arr_airport,
        'time_off':             f.time_off,
        'time_on':              f.time_on,
        'flight_time_display':  display,
        'total_flight_time':    flight_hr_tenth,
        'total_loggable_hours': total_loggable,
        'role':                 f.role,
        'taxi':                 taxi,
        'instrument':           f.instrument or 0,
        'landings':             f.landings if f.landings is not None else 1,
        'cfi':                  f.cfi or 0,
        'solo':                 f.solo or 0,
        'holds':                f.holds or 0,
        'approaches_ils':       f.approaches_ils or 0,
        'approaches_gps':       f.approaches_gps or 0,
        'night_hours':          f.night_hours or 0,
        'night_landings':       f.night_landings or 0,
        'cross_country':        f.cross_country or 0,
        'international':        bool(f.international),
        'ocean_crossing':       bool(f.ocean_crossing),
        'comments':             f.comments or '',
        'air_dist':             f.air_dist,
        'gnd_dist':             f.gnd_dist,
        'ocr_source':           f.ocr_source or '',
        'mfb_flight_id':        f.mfb_flight_id,
        'mfb_status':           f.mfb_status or '',
        'mfb_message':          f.mfb_message or '',
    }

# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

# ── Flights API ───────────────────────────────────────────────────────────────

@app.route('/api/autocalc', methods=['POST'])
def api_autocalc():
    d = request.json or {}
    return jsonify(autocalc(
        d.get('dep_airport'), d.get('arr_airport'), d.get('date'),
        d.get('time_off'), d.get('time_on'), float(d.get('taxi', 0.3) or 0.3),
    ))

@app.route('/api/pfm-lookup', methods=['GET'])
def pfm_lookup_route():
    """Suggest dep/arr airports for a flight by cross-referencing the PFM schedule.
    Never trust this blindly: it flags diversion_suspected when the FMS-reported
    ground distance doesn't match the scheduled route, since the photographed
    FMS flight log is the actual source of truth, not the schedule."""
    tail     = request.args.get('tail', '')
    date_str = request.args.get('date', '')       # YYYY-MM-DD
    time_off = request.args.get('time_off', '')
    gnd_dist = request.args.get('gnd_dist', type=float)

    if not date_str:
        return jsonify({'error': 'date required (YYYY-MM-DD)'}), 400
    try:
        y, m, dnum = (int(x) for x in date_str.split('-'))
        flight_date = date(y, m, dnum)
    except Exception:
        return jsonify({'error': 'date must be YYYY-MM-DD'}), 400

    result = (pfm_lookup.find_scheduled_leg(tail, flight_date, time_off) if tail
              else pfm_lookup.find_scheduled_leg_by_date(flight_date, time_off))
    if not result:
        return jsonify({'found': False})

    out = {'found': True, **result, 'diversion_suspected': False}
    suspected, note, expected_nm = _diversion_check(result['dep_airport'], result['arr_airport'], gnd_dist)
    out['expected_distance_nm'] = expected_nm
    if suspected:
        out['diversion_suspected'] = True
        out['diversion_note'] = note
    return jsonify(out)

@app.route('/api/flightaware-lookup', methods=['GET'])
def flightaware_lookup_route():
    """Looks up a tail's actual flights for one day via FlightAware AeroAPI —
    the alternative to photographing an FMS display, for aircraft (e.g. the
    Cirrus) that don't have one but do fly with ADS-B Out."""
    tail = request.args.get('tail', '').strip()
    date_str = request.args.get('date', '')
    if not tail:
        return jsonify({'error': 'tail required'}), 400
    if not date_str:
        return jsonify({'error': 'date required (YYYY-MM-DD)'}), 400
    try:
        y, m, dnum = (int(x) for x in date_str.split('-'))
        flight_date = date(y, m, dnum)
    except Exception:
        return jsonify({'error': 'date must be YYYY-MM-DD'}), 400

    try:
        flights = flightaware.flights_for_day(tail.upper(), flight_date)
    except flightaware.FlightAwareError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'flights': flights})

@app.route('/api/flights', methods=['GET'])
def list_flights():
    flights = Flight.query.order_by(Flight.created_at.desc()).limit(50).all()
    return jsonify([flight_to_dict(f) for f in flights])

MFB_CWD    = os.environ.get('MFB_CWD', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'mfb'))
MFB_PYTHON = os.environ.get('MFB_PYTHON', os.path.join(MFB_CWD, '.venv', 'bin', 'python'))

def _push_to_mfb(flight, force=False):
    """Commits this flight to the pilot's real MyFlightbook logbook, via a
    subprocess call into the separate mfb project (own venv/auth/token)."""
    payload = {
        'tail': flight.tail, 'date': flight.date,
        'dep_airport': flight.dep_airport, 'arr_airport': flight.arr_airport,
        'role': flight.role, 'time_off': flight.time_off, 'time_on': flight.time_on,
        'taxi': flight.taxi or 0, 'instrument': flight.instrument or 0,
        'night_hours': flight.night_hours or 0, 'night_landings': flight.night_landings or 0,
        'landings': flight.landings or 1, 'cross_country': flight.cross_country or 0,
        'total_loggable_hours': flight_to_dict(flight)['total_loggable_hours'],
        'comments': flight.comments or '',
        'cfi': flight.cfi or 0, 'solo': flight.solo or 0, 'holds': flight.holds or 0,
        'approaches_ils': flight.approaches_ils or 0, 'approaches_gps': flight.approaches_gps or 0,
        'force': force,
    }
    try:
        proc = subprocess.run(
            [MFB_PYTHON, '-m', 'mfb.push_flight'], cwd=MFB_CWD,
            input=json.dumps(payload), capture_output=True, text=True, timeout=30,
        )
        return json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else \
            {'status': 'error', 'message': proc.stderr[-500:] or 'no output from mfb push'}
    except subprocess.TimeoutExpired:
        return {'status': 'error', 'message': 'MyFlightbook push timed out'}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

@app.route('/api/flights', methods=['POST'])
def create_flight():
    """Creates the local record from the confirmed table AND immediately pushes
    it to MyFlightbook — this is the single 'Submit' action."""
    d = request.json or {}
    required = ('date', 'tail', 'dep_airport', 'arr_airport', 'time_off', 'time_on')
    missing = [f for f in required if not (d.get(f) or '').strip()]
    if missing:
        return jsonify({'status': 'error', 'message': f"Missing required fields: {', '.join(missing)}"}), 400

    flight = Flight(
        date=d['date'], tail=d['tail'].upper(),
        dep_airport=d['dep_airport'].upper(), arr_airport=d['arr_airport'].upper(),
        time_off=d['time_off'], time_on=d['time_on'],
        role=(d.get('role') or 'PIC').upper(),
        taxi=float(d.get('taxi', 0.3) if d.get('taxi') is not None else 0.3),
        instrument=float(d.get('instrument') or 0),
        landings=int(d.get('landings', 1) if d.get('landings') is not None else 1),
        cfi=float(d.get('cfi') or 0),
        solo=float(d.get('solo') or 0),
        holds=int(d.get('holds') or 0),
        approaches_ils=int(d.get('approaches_ils') or 0),
        approaches_gps=int(d.get('approaches_gps') or 0),
        night_hours=float(d.get('night_hours') or 0),
        night_landings=int(d.get('night_landings') or 0),
        cross_country=float(d.get('cross_country') or 0),
        international=bool(d.get('international')),
        ocean_crossing=bool(d.get('ocean_crossing')),
        comments=d.get('comments', ''),
        air_dist=d.get('air_dist'),
        gnd_dist=d.get('gnd_dist'),
        ocr_source=d.get('ocr_source', ''),
    )
    db.session.add(flight)
    db.session.flush()

    result = _push_to_mfb(flight)
    flight.mfb_status = result.get('status', 'error')
    flight.mfb_message = result.get('message', '')
    if result.get('status') == 'created':
        flight.mfb_flight_id = result.get('flight_id')
    elif result.get('status') == 'duplicate':
        flight.mfb_flight_id = result.get('existing_flight_id')

    db.session.commit()
    return jsonify({**flight_to_dict(flight), 'push_result': result})

@app.route('/api/flights/<int:flight_id>/push-to-mfb', methods=['POST'])
def retry_push(flight_id):
    """Retry pushing a flight that previously errored. Refuses to re-push a
    flight already successfully created unless force=true is passed."""
    flight = Flight.query.get_or_404(flight_id)
    force = bool((request.json or {}).get('force'))
    if flight.mfb_status == 'created' and not force:
        return jsonify({'status': 'already_pushed', 'flight_id': flight.mfb_flight_id})

    result = _push_to_mfb(flight, force=force)
    flight.mfb_status = result.get('status', 'error')
    flight.mfb_message = result.get('message', '')
    if result.get('status') == 'created':
        flight.mfb_flight_id = result.get('flight_id')
    elif result.get('status') == 'duplicate':
        flight.mfb_flight_id = result.get('existing_flight_id')
    db.session.commit()
    return jsonify({**flight_to_dict(flight), 'push_result': result})

@app.route('/api/flights/<int:flight_id>', methods=['DELETE'])
def delete_flight(flight_id):
    flight = Flight.query.get_or_404(flight_id)
    if flight.mfb_status == 'created':
        return jsonify({'status': 'error', 'message': 'Already pushed to MyFlightbook — delete it there first.'}), 400
    db.session.delete(flight)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/flights/export', methods=['GET'])
def export_flights():
    flights = [flight_to_dict(f) for f in Flight.query.order_by(Flight.created_at.desc()).all()]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['Date', 'Tail', 'Dep', 'Arr', 'Off', 'On', 'Total', 'Role', 'Landings',
                'Night Hrs', 'Night Ldg', 'Instrument', 'XC', 'Intl', 'Ocean',
                'CFI', 'Solo', 'Holds', 'App ILS', 'App GPS',
                'Comments', 'MFB Status', 'MFB Flight ID'])
    for f in flights:
        w.writerow([f['date'], f['tail'], f['dep_airport'], f['arr_airport'], f['time_off'], f['time_on'],
                    f['total_loggable_hours'], f['role'], f['landings'], f['night_hours'], f['night_landings'],
                    f['instrument'], f['cross_country'], f['international'], f['ocean_crossing'],
                    f['cfi'], f['solo'], f['holds'], f['approaches_ils'], f['approaches_gps'],
                    f['comments'], f['mfb_status'], f['mfb_flight_id']])
    out.seek(0)
    return send_file(io.BytesIO(out.getvalue().encode()), mimetype='text/csv',
                      as_attachment=True, download_name='flightlog_export.csv')

# ── ACARS OCR ─────────────────────────────────────────────────────────────────

MAX_OCR_DIM = 1200  # cap before the 3x upscale

def _ocr_variants(img):
    """Yield (preprocessed_image, psm) pairs across rotations and preprocessing strategies."""
    # Respect EXIF orientation then try all 4 rotations — phone photos are often sideways
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    w, h = img.size
    if max(w, h) > MAX_OCR_DIM:
        scale = MAX_OCR_DIM / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    for angle in [0, 90, 270, 180]:
        rot = img.rotate(angle, expand=True) if angle else img
        arr = np.array(rot.convert('RGB'))
        r, g, b = arr[:,:,0].astype(float), arr[:,:,1].astype(float), arr[:,:,2].astype(float)

        # Two channel strategies:
        # - green boost: finds green text on dark bg
        # - plain gray: finds white text on colored bg
        boosted = np.maximum(g - 0.5*b, (r+g)/2.0 - b)
        plain   = 0.299*r + 0.587*g + 0.114*b

        for raw in (boosted, plain):
            from PIL import ImageEnhance
            src = Image.fromarray(np.clip(raw, 0, 255).astype(np.uint8), 'L')
            src = ImageEnhance.Contrast(src).enhance(2.5)
            yield src, '6'
            yield src, '11'
            yield src, '4'

def extract_times(text):
    """Extract time_off, time_on from Honeywell TIMES/FUELS OCR text."""
    # Fix digit lookalikes in a candidate time string only — NOT on the whole line
    # (applying O→0 to the whole line breaks keyword matching: "OFF" → "0FF")
    def fix_digits(s):
        return (s.replace('O','0').replace('o','0').replace('l','1')
                 .replace('I','1').replace('|','1').replace('S','5')
                 .replace('B','8').replace('G','6'))
        # note: Z not replaced — "0336Z" the Z is not part of the 4-digit match

    def valid_time(s):
        try: return 0 <= int(s[:2]) <= 23 and 0 <= int(s[2:]) <= 59
        except: return False

    def times_in(line):
        candidates = []
        # Standard 4-digit match
        for m in re.finditer(r'[0-9OoIlBGS]{4}', line):
            fixed = fix_digits(m.group(0))
            if re.match(r'^\d{4}$', fixed) and valid_time(fixed):
                candidates.append(fixed)
        # 3-digit match — OCR often drops the leading zero (e.g. "457Z" → "0457")
        for m in re.finditer(r'(?<!\d)([0-9OoIlBGS]{3})Z', line):
            fixed = '0' + fix_digits(m.group(1))
            if valid_time(fixed) and fixed not in candidates:
                candidates.append(fixed)
        return candidates

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # Primary: find OFF keyword, then find ON keyword separately
    time_off, time_on = None, None

    for i, line in enumerate(lines):
        if re.search(r'\bOFF\b', line.upper()):
            for j in range(i, min(i+3, len(lines))):
                t = times_in(lines[j])
                if t:
                    time_off = t[0]
                    break

    for i, line in enumerate(lines):
        if re.search(r'\bON\b', line.upper()) and not re.search(r'\bOFF\b', line.upper()):
            for j in range(i, min(i+3, len(lines))):
                t = times_in(lines[j])
                if t:
                    time_on = t[0]
                    break

    if time_off or time_on:
        return time_off, time_on

    # Fallback: positional — OUT IN OFF ON order
    all_t = [t for line in lines for t in times_in(line)]
    if len(all_t) >= 4: return all_t[2], all_t[3]
    if len(all_t) >= 2: return all_t[0], all_t[1]
    return None, None

# ── Local vision model (Ollama / gemma3:4b) ────────────────────────────────────
OLLAMA_URL     = os.environ.get('OLLAMA_URL', 'http://localhost:11434')
VISION_MODEL   = os.environ.get('ACARS_VISION_MODEL', 'gemma3:4b')
VISION_TIMEOUT = int(os.environ.get('ACARS_VISION_TIMEOUT', '180'))  # seconds (CPU is slow)

def _valid_hhmm(s):
    try: return 0 <= int(s[:2]) <= 23 and 0 <= int(s[2:]) <= 59
    except: return False

def _normalize_hhmm(s):
    """Coerce a model value to a validated 4-digit HHMM string, else None."""
    if s is None: return None
    d = re.sub(r'\D', '', str(s))
    if len(d) == 3: d = '0' + d   # model dropped a leading zero
    return d if len(d) == 4 and _valid_hhmm(d) else None

def _normalize_nm(v):
    try:
        n = float(str(v).replace(',', ''))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None

def _normalize_date(v):
    """Coerce a model-read date to YYYY-MM-DD, else None. Accepts a handful of
    formats a vision model might emit (it's told to use ISO but may not)."""
    if not v:
        return None
    s = str(v).strip()
    for fmt in ('%Y-%m-%d', '%d%b%y', '%d %b %y', '%d%b%Y', '%m/%d/%Y', '%m/%d/%y'):
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None

def _normalize_code(v):
    if not v:
        return None
    s = re.sub(r'[^A-Za-z]', '', str(v)).upper()
    return s if 3 <= len(s) <= 4 else None

# ── Claude vision (primary OCR path when ANTHROPIC_API_KEY is set) ────────────

_anthropic_client = anthropic.Anthropic() if os.environ.get('ANTHROPIC_API_KEY') else None

_ACARS_SCHEMA = {
    "type": "object",
    "properties": {
        "time_off":    {"type": ["string", "null"], "description": "T/O or wheels-OFF time, 4-digit 24h UTC HHMM"},
        "time_on":     {"type": ["string", "null"], "description": "LDG or wheels-ON time, 4-digit 24h UTC HHMM"},
        "air_dist_nm": {"type": ["number", "null"], "description": "AIR DIST value in nautical miles, if shown"},
        "gnd_dist_nm": {"type": ["number", "null"], "description": "GND DIST value in nautical miles, if shown"},
        "flight_date": {"type": ["string", "null"], "description": "Flight date, if shown on screen, as YYYY-MM-DD"},
        "tail":        {"type": ["string", "null"], "description": "Aircraft registration/tail number, if shown"},
        "dep_airport": {"type": ["string", "null"], "description": "Departure airport ICAO/FAA code, if shown"},
        "arr_airport": {"type": ["string", "null"], "description": "Destination airport ICAO/FAA code, if shown"},
    },
    "required": ["time_off", "time_on", "air_dist_nm", "gnd_dist_nm", "flight_date", "tail", "dep_airport", "arr_airport"],
    "additionalProperties": False,
}

_VISION_PROMPT_TEXT = (
    "This is a photo of an aircraft cockpit display showing flight timing data — "
    "either an ACARS/Honeywell TIMES report or a Collins FMS 'FLIGHT LOG' perf page. "
    "Read the wheels-OFF (takeoff/T/O) time and wheels-ON (landing/LDG) time, each a "
    "4-digit 24-hour UTC time. If the screen is a FLIGHT LOG page, also read the AIR "
    "DIST and GND DIST values in nautical miles. Some screens also show the flight "
    "date, aircraft tail/registration number, and departure/destination airport "
    "codes — read any of those that are clearly visible too. Read every digit "
    "carefully. Use null for anything not shown or you can't read with confidence — "
    "do not guess."
)

def _claude_extract(img):
    """Ask Claude to read the cockpit display. Returns a dict with keys time_off,
    time_on, air_dist, gnd_dist, flight_date, tail, dep_airport, arr_airport, raw —
    every value None on any failure (no key configured, network error, refusal) so
    the caller falls back to the local vision model."""
    empty = {'time_off': None, 'time_on': None, 'air_dist': None, 'gnd_dist': None,
              'flight_date': None, 'tail': None, 'dep_airport': None, 'arr_airport': None, 'raw': ''}
    if _anthropic_client is None:
        return empty
    try:
        buf = io.BytesIO()
        img.convert('RGB').save(buf, format='JPEG', quality=90)
        b64 = base64.b64encode(buf.getvalue()).decode()
        response = _anthropic_client.messages.create(
            model="claude-opus-4-8",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": _VISION_PROMPT_TEXT},
                ],
            }],
            output_config={"format": {"type": "json_schema", "schema": _ACARS_SCHEMA}},
        )
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
        result = {
            'time_off':    _normalize_hhmm(data.get('time_off')),
            'time_on':     _normalize_hhmm(data.get('time_on')),
            'air_dist':    _normalize_nm(data.get('air_dist_nm')),
            'gnd_dist':    _normalize_nm(data.get('gnd_dist_nm')),
            'flight_date': _normalize_date(data.get('flight_date')),
            'tail':        _normalize_code(data.get('tail')),
            'dep_airport': _normalize_code(data.get('dep_airport')),
            'arr_airport': _normalize_code(data.get('arr_airport')),
            'raw': text,
        }
        print(f'ACARS claude {result}', flush=True)
        return result
    except Exception as e:
        print(f'ACARS claude error: {e}', flush=True)
        return empty


def _gemma_extract(img):
    """Ask the local vision model for the same field set as _claude_extract.
    Returns the same dict shape; unset fields are None (expected — the plain
    ACARS TIMES/FUELS page doesn't show distances/date/tail/airports)."""
    empty = {'time_off': None, 'time_on': None, 'air_dist': None, 'gnd_dist': None,
              'flight_date': None, 'tail': None, 'dep_airport': None, 'arr_airport': None, 'raw': ''}
    try:
        buf = io.BytesIO()
        img.convert('RGB').save(buf, format='JPEG', quality=90)
        b64 = base64.b64encode(buf.getvalue()).decode()
        prompt = (
            _VISION_PROMPT_TEXT + ' Respond with ONLY JSON of the form '
            '{"time_off":"HHMM","time_on":"HHMM","air_dist_nm":123,"gnd_dist_nm":123,'
            '"flight_date":"YYYY-MM-DD","tail":"N123AB","dep_airport":"KIWA","arr_airport":"KIWA"}. '
            'Use null for any value you cannot read with confidence or that isn\'t shown.'
        )
        payload = json.dumps({
            "model": VISION_MODEL, "prompt": prompt, "images": [b64],
            "stream": False, "format": "json", "keep_alive": "30m",
            "options": {"temperature": 0},
        }).encode()
        req = urllib.request.Request(
            OLLAMA_URL + '/api/generate', data=payload,
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=VISION_TIMEOUT) as r:
            content = json.loads(r.read().decode()).get('response', '')
        data = json.loads(content)                       # format=json guarantees JSON
        result = {
            'time_off':    _normalize_hhmm(data.get('time_off')),
            'time_on':     _normalize_hhmm(data.get('time_on')),
            'air_dist':    _normalize_nm(data.get('air_dist_nm')),
            'gnd_dist':    _normalize_nm(data.get('gnd_dist_nm')),
            'flight_date': _normalize_date(data.get('flight_date')),
            'tail':        _normalize_code(data.get('tail')),
            'dep_airport': _normalize_code(data.get('dep_airport')),
            'arr_airport': _normalize_code(data.get('arr_airport')),
            'raw': content,
        }
        print(f'ACARS gemma {result}', flush=True)
        return result
    except Exception as e:
        print(f'ACARS gemma error: {e}', flush=True)
        return empty


def _merge(primary, fallback, key):
    return primary.get(key) if primary.get(key) is not None else fallback.get(key)


def _diversion_check(dep_code, arr_code, gnd_dist):
    """Compares a scheduled route's distance to the FMS-reported ground distance.
    Returns (diversion_suspected, note_or_None, expected_distance_nm_or_None)."""
    dep = geo.find_airport(dep_code)
    arr = geo.find_airport(arr_code)
    if not (dep and arr):
        return False, None, None
    expected_nm = geo.route_distance_nm(dep, arr)
    if gnd_dist:
        diff = abs(gnd_dist - expected_nm)
        if diff > max(75, 0.25 * expected_nm):
            note = (f"Scheduled {dep_code}-{arr_code} is ~{round(expected_nm)}nm, but FMS ground "
                     f"distance was {gnd_dist:.0f}nm. Verify airports/tail — possible diversion.")
            return True, note, round(expected_nm, 1)
    return False, None, round(expected_nm, 1)


def _infer_flight_date(time_off_hhmm):
    """Best-guess flight date when the photo doesn't show one, assuming the pic
    is uploaded shortly after landing: if today's occurrence of the OFF clock
    time is still in the future (relative to right now, UTC), the flight must
    have been yesterday — e.g. an evening-Zulu flight uploaded just after the
    UTC calendar date rolls over at midnight. Falls back to plain 'today' if
    time_off wasn't read either."""
    now = datetime.now(timezone.utc)
    if not time_off_hhmm or not _valid_hhmm(time_off_hhmm):
        return now.date()
    off_min = int(time_off_hhmm[:2]) * 60 + int(time_off_hhmm[2:])
    candidate_today = datetime.combine(now.date(), time.min, tzinfo=timezone.utc) + timedelta(minutes=off_min)
    return (now.date() - timedelta(days=1)) if candidate_today > now else now.date()


@app.route('/api/parse-acars', methods=['POST'])
def parse_acars():
    """Full pipeline for a submitted photo: vision OCR (Claude, then local Gemma,
    then Tesseract as last resort for times only), merged with a PFM trip-sheet
    lookup for whichever of date/tail/dep/arr the photo didn't show. Every field
    in the response carries a matching '<field>_source' so the UI can show the
    pilot where each prefilled value came from."""
    if 'image' not in request.files:
        return jsonify({'error': 'No image'}), 400
    raw = request.files['image'].read()
    try:
        img = Image.open(io.BytesIO(raw))
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    empty = {'time_off': None, 'time_on': None, 'air_dist': None, 'gnd_dist': None,
              'flight_date': None, 'tail': None, 'dep_airport': None, 'arr_airport': None, 'raw': ''}

    # 1) Primary: Claude vision, when a key is configured. Trust it only when
    #    both times validate — a partial read still falls through to Ollama.
    claude = _claude_extract(img)
    gemma = empty
    source = None
    if claude['time_off'] and claude['time_on']:
        source = 'claude-opus-4-8'
    else:
        # 2) Fallback: local vision model (offline-capable).
        gemma = _gemma_extract(img)
        if gemma['time_off'] and gemma['time_on']:
            source = VISION_MODEL

    best = {**empty}
    if source:
        best = claude if source == 'claude-opus-4-8' else gemma
    else:
        # 3) Last resort: Tesseract OCR pipeline (times only).
        tess = {'time_off': None, 'time_on': None, '_score': -1, 'raw': ''}
        for i, (bw, psm) in enumerate(_ocr_variants(img)):
            text = pytesseract.image_to_string(bw, config=f'--oem 3 --psm {psm} -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ.:/+ ')
            off, on = extract_times(text)
            score = (1 if off else 0) + (1 if on else 0)
            print(f'ACARS v{i} psm={psm} score={score} off={off} on={on} | {repr(text[:120])}', flush=True)
            if score > tess['_score']:
                tess = {'time_off': off, 'time_on': on, '_score': score, 'raw': text}
            if score == 2:
                break
        tess.pop('_score')
        best = {**empty, **tess}
        # Fill any gap with a validated single value a vision model did manage to read.
        best['time_off'] = best['time_off'] or claude['time_off'] or gemma['time_off']
        best['time_on']  = best['time_on']  or claude['time_on']  or gemma['time_on']
        best['air_dist'] = claude['air_dist'] or gemma['air_dist']
        best['gnd_dist'] = claude['gnd_dist'] or gemma['gnd_dist']
        best['flight_date'] = claude['flight_date'] or gemma['flight_date']
        best['tail'] = claude['tail'] or gemma['tail']
        best['dep_airport'] = claude['dep_airport'] or gemma['dep_airport']
        best['arr_airport'] = claude['arr_airport'] or gemma['arr_airport']
        source = 'tesseract' + ('+claude' if (claude['time_off'] or claude['time_on']) else '') + \
                 ('+' + VISION_MODEL if (gemma['time_off'] or gemma['time_on']) else '')

    out = {
        'time_off': best['time_off'], 'time_on': best['time_on'],
        'air_dist': best['air_dist'], 'gnd_dist': best['gnd_dist'],
        'date': best['flight_date'], 'date_source': 'photo' if best['flight_date'] else None,
        'tail': best['tail'], 'tail_source': 'photo' if best['tail'] else None,
        'dep_airport': best['dep_airport'], 'dep_source': 'photo' if best['dep_airport'] else None,
        'arr_airport': best['arr_airport'], 'arr_source': 'photo' if best['arr_airport'] else None,
        '_ocr_raw': best.get('raw', ''), '_source': source,
        'diversion_suspected': False,
    }
    if not out['date']:
        out['date'] = _infer_flight_date(out['time_off']).isoformat()
        out['date_source'] = 'default'

    # PFM trip-sheet fills whatever the photo didn't show — including the tail
    # itself, since the FMS display usually doesn't show it either. This DB is
    # Paul's own personal schedule sync, so "any leg on this date" is safe to
    # assume as "my leg on this date" when there's no tail yet to narrow by.
    flight_date = datetime.strptime(out['date'], '%Y-%m-%d').date()
    sched = None
    if out['tail']:
        if not out['dep_airport'] or not out['arr_airport']:
            sched = pfm_lookup.find_scheduled_leg(out['tail'], flight_date, out['time_off'])
    else:
        sched = pfm_lookup.find_scheduled_leg_by_date(flight_date, out['time_off'])
        if sched:
            out['tail'], out['tail_source'] = sched['tail'], 'schedule'

    if sched:
        if not out['dep_airport']:
            out['dep_airport'], out['dep_source'] = sched['dep_airport'], 'schedule'
        if not out['arr_airport']:
            out['arr_airport'], out['arr_source'] = sched['arr_airport'], 'schedule'
        suspected, note, _ = _diversion_check(out['dep_airport'], out['arr_airport'], out['gnd_dist'])
        if suspected:
            out['diversion_suspected'] = True
            out['diversion_note'] = note

    if out['dep_airport'] and out['arr_airport']:
        out['autocalc'] = autocalc(out['dep_airport'], out['arr_airport'], out['date'],
                                    out['time_off'], out['time_on'])

    return jsonify(out)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050, debug=True)
