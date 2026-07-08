import os, csv, io, re, json, base64, urllib.request
from flask import Flask, request, jsonify, render_template, send_file
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from PIL import Image, ImageFilter
import pytesseract
import numpy as np

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///flightlog.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class Trip(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    name            = db.Column(db.String(100), default='')
    date            = db.Column(db.String(20),  default='')
    aircraft        = db.Column(db.String(20),  default='')
    hours_to_date   = db.Column(db.Float,   default=0)
    landings_to_date= db.Column(db.Integer, default=0)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    legs = db.relationship('Leg', backref='trip', lazy=True,
                           cascade='all, delete-orphan', order_by='Leg.leg_number')

class Leg(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    trip_id    = db.Column(db.Integer, db.ForeignKey('trip.id'), nullable=False)
    leg_number = db.Column(db.Integer, nullable=False)
    date_z     = db.Column(db.String(10), default='')
    time_off   = db.Column(db.String(4),  default='')  # HHMM zulu
    time_on    = db.Column(db.String(4),  default='')  # HHMM zulu

with app.app_context():
    db.create_all()

# ── Math helpers ──────────────────────────────────────────────────────────────

def hhmm_to_minutes(hhmm):
    try:
        s = str(hhmm).zfill(4)
        return int(s[:2]) * 60 + int(s[2:])
    except:
        return None

def calc_flight_time(time_off, time_on):
    """Returns (total_minutes, 'H:MM') handling midnight crossing."""
    off = hhmm_to_minutes(time_off)
    on  = hhmm_to_minutes(time_on)
    if off is None or on is None:
        return None, ''
    delta = on - off
    if delta < 0:
        delta += 1440
    return delta, f"{delta // 60}:{delta % 60:02d}"

def minutes_to_tenth(total_minutes):
    """Standard aviation minute→tenth-of-hour table."""
    if total_minutes is None:
        return None
    hours = total_minutes // 60
    rem   = total_minutes % 60
    if   rem <=  2: t = 0.0
    elif rem <=  8: t = 0.1
    elif rem <= 14: t = 0.2
    elif rem <= 20: t = 0.3
    elif rem <= 26: t = 0.4
    elif rem <= 32: t = 0.5
    elif rem <= 38: t = 0.6
    elif rem <= 44: t = 0.7
    elif rem <= 50: t = 0.8
    elif rem <= 56: t = 0.9
    else:           t = 1.0
    return round(hours + t, 1)

# ── Serialisers ───────────────────────────────────────────────────────────────

def leg_to_dict(leg):
    total_min, display = calc_flight_time(leg.time_off, leg.time_on)
    return {
        'id':                   leg.id,
        'leg_number':           leg.leg_number,
        'date_z':               leg.date_z,
        'time_off':             leg.time_off,
        'time_on':              leg.time_on,
        'flight_time_display':  display,
        'flight_time_minutes':  total_min,
        'flight_hr_tenth':      minutes_to_tenth(total_min),
    }

def trip_to_dict(trip):
    legs        = [leg_to_dict(l) for l in trip.legs]
    today_tenth = round(sum(l['flight_hr_tenth'] for l in legs
                            if l['flight_hr_tenth'] is not None), 1)
    return {
        'id':               trip.id,
        'name':             trip.name,
        'date':             trip.date,
        'aircraft':         trip.aircraft,
        'hours_to_date':    trip.hours_to_date,
        'landings_to_date': trip.landings_to_date,
        'legs':             legs,
        'today_hours':      today_tenth,
        'today_cycles':     len(legs),
        'total_hours':      round(trip.hours_to_date + today_tenth, 1),
        'total_landings':   trip.landings_to_date + len(legs),
    }

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/trips', methods=['GET'])
def get_trips():
    trips = Trip.query.order_by(Trip.created_at.desc()).all()
    return jsonify([{'id': t.id, 'name': t.name, 'date': t.date,
                     'aircraft': t.aircraft} for t in trips])

@app.route('/api/trips', methods=['POST'])
def create_trip():
    d = request.json
    trip = Trip(
        name=d.get('name',''),
        date=d.get('date',''),
        aircraft=d.get('aircraft',''),
        hours_to_date=float(d.get('hours_to_date') or 0),
        landings_to_date=int(d.get('landings_to_date') or 0),
    )
    db.session.add(trip); db.session.commit()
    return jsonify(trip_to_dict(trip))

@app.route('/api/trips/<int:trip_id>', methods=['GET'])
def get_trip(trip_id):
    return jsonify(trip_to_dict(Trip.query.get_or_404(trip_id)))

@app.route('/api/trips/<int:trip_id>', methods=['PUT'])
def update_trip(trip_id):
    trip = Trip.query.get_or_404(trip_id)
    d = request.json
    for f in ('name','date','aircraft'):
        if f in d: setattr(trip, f, d[f])
    if 'hours_to_date'    in d: trip.hours_to_date    = float(d['hours_to_date'] or 0)
    if 'landings_to_date' in d: trip.landings_to_date = int(d['landings_to_date'] or 0)
    db.session.commit()
    return jsonify(trip_to_dict(trip))

@app.route('/api/trips/<int:trip_id>/legs', methods=['POST'])
def add_leg(trip_id):
    trip = Trip.query.get_or_404(trip_id)
    d = request.json or {}
    next_num = max((l.leg_number for l in trip.legs), default=0) + 1
    leg = Leg(trip_id=trip_id,
              leg_number=d.get('leg_number', next_num),
              date_z=d.get('date_z',''),
              time_off=d.get('time_off',''),
              time_on=d.get('time_on',''))
    db.session.add(leg); db.session.commit()
    return jsonify(trip_to_dict(trip))

@app.route('/api/legs/<int:leg_id>', methods=['PUT'])
def update_leg(leg_id):
    leg = Leg.query.get_or_404(leg_id)
    d = request.json
    for f in ('date_z','time_off','time_on'):
        if f in d: setattr(leg, f, d[f])
    db.session.commit()
    return jsonify(trip_to_dict(Trip.query.get(leg.trip_id)))

@app.route('/api/legs/<int:leg_id>', methods=['DELETE'])
def delete_leg(leg_id):
    leg = Leg.query.get_or_404(leg_id)
    trip_id = leg.trip_id
    db.session.delete(leg); db.session.commit()
    return jsonify(trip_to_dict(Trip.query.get(trip_id)))

@app.route('/api/trips', methods=['DELETE'])
def delete_all_trips():
    Leg.query.delete()
    Trip.query.delete()
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/trips/<int:trip_id>/export', methods=['GET'])
def export_trip(trip_id):
    td = trip_to_dict(Trip.query.get_or_404(trip_id))
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['Aircraft', td['aircraft'], 'Date', td['date']])
    w.writerow(['Hours To Date', td['hours_to_date'],
                'Landings To Date', td['landings_to_date']])
    w.writerow([])
    w.writerow(['Leg','Date (Z)','Time Off','Time On','Flight Time','Hr/Tenth'])
    for l in td['legs']:
        w.writerow([l['leg_number'], l['date_z'], l['time_off'], l['time_on'],
                    l['flight_time_display'], l['flight_hr_tenth']])
    w.writerow([])
    w.writerow(['Today Hr/Tenth', td['today_hours'],
                'Today Cycles',   td['today_cycles']])
    w.writerow(['Total Hours',    td['total_hours'],
                'Total Landings', td['total_landings']])
    out.seek(0)
    return send_file(io.BytesIO(out.getvalue().encode()), mimetype='text/csv',
                     as_attachment=True,
                     download_name=f"flightlog_{td['aircraft']}_{td['date']}.csv")

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

def _gemma_extract(img):
    """Ask the local vision model for OFF/ON times. Returns (off, on, raw_json)."""
    try:
        buf = io.BytesIO()
        img.convert('RGB').save(buf, format='JPEG', quality=90)
        b64 = base64.b64encode(buf.getvalue()).decode()
        prompt = (
            "This image is a photo of an aircraft ACARS / Honeywell flight timing "
            "report. Read the wheels-OFF (takeoff) time and the wheels-ON (landing) "
            "time. Each is a 4-digit 24-hour UTC time in HHMM format. Respond with "
            'ONLY JSON of the form {"time_off":"HHMM","time_on":"HHMM"}. Use null for '
            "any value you cannot read with confidence. Do not guess."
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
        off = _normalize_hhmm(data.get('time_off'))
        on  = _normalize_hhmm(data.get('time_on'))
        print(f'ACARS gemma off={off} on={on} | {content[:160]}', flush=True)
        return off, on, content
    except Exception as e:
        print(f'ACARS gemma error: {e}', flush=True)
        return None, None, ''


@app.route('/api/parse-acars', methods=['POST'])
def parse_acars():
    if 'image' not in request.files:
        return jsonify({'error': 'No image'}), 400
    raw = request.files['image'].read()
    try:
        img = Image.open(io.BytesIO(raw))
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    # 1) Primary: local vision model. Trust it only when BOTH times validate.
    g_off, g_on, g_raw = _gemma_extract(img)
    if g_off and g_on:
        return jsonify({'time_off': g_off, 'time_on': g_on,
                        '_ocr_raw': g_raw, '_source': VISION_MODEL})

    # 2) Fallback: Tesseract OCR pipeline (also covers a partial gemma read).
    best = {'time_off': None, 'time_on': None, '_ocr_raw': '', '_score': -1}
    for i, (bw, psm) in enumerate(_ocr_variants(img)):
        text = pytesseract.image_to_string(bw, config=f'--oem 3 --psm {psm} -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ.:/+ ')
        off, on = extract_times(text)
        score = (1 if off else 0) + (1 if on else 0)
        if i == 4:  # 270° rotation (90° CW) — should be right-side up
            bw.save('/tmp/acars_debug_rot.png')
        print(f'ACARS v{i} psm={psm} score={score} off={off} on={on} | {repr(text[:120])}', flush=True)
        if score > best['_score']:
            best = {'time_off': off, 'time_on': on, '_ocr_raw': text, '_score': score}
        if score == 2:
            break  # can't do better

    best.pop('_score')
    # Fill any gap with a validated single value gemma did manage to read.
    best['time_off'] = best['time_off'] or g_off
    best['time_on']  = best['time_on']  or g_on
    best['_source']  = 'tesseract' + ('+' + VISION_MODEL if (g_off or g_on) else '')
    return jsonify(best)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050, debug=True)
