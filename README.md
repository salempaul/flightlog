# flightlog

Flask app that turns a photo of an FMS/ACARS screen into a MyFlightBook
logbook entry: OCRs the photo, lets you confirm/edit the parsed fields in
a web UI, then pushes the confirmed flight to MyFlightBook.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Also needs the system `tesseract-ocr` binary on `PATH` (used as the last-resort
OCR fallback — see below): `apt install tesseract-ocr` on Debian/Ubuntu.

### Sibling dependency: `mfb/`

Pushing a confirmed flight shells out to a **separate** `mfb` project
(its own venv, own MyFlightBook OAuth setup — see `mfb/README.md`) rather
than talking to MyFlightBook directly. By default this code looks for it
at `../mfb` next to this repo; if you clone it somewhere else, point at it
with:

```
MFB_CWD=/path/to/mfb        # defaults to ../mfb relative to this file
MFB_PYTHON=/path/to/python  # defaults to $MFB_CWD/.venv/bin/python
```

**Set up `mfb/` and its MyFlightBook OAuth credentials first** — flight
submission will fail until that's working (see that repo's README for the
manual OAuth client registration step, which can't be automated).

### OCR — pick at least one

The FMS-photo parser tries three tiers in order, first one available wins:

1. **Claude vision** (best quality) — set `ANTHROPIC_API_KEY` in the
   environment.
2. **Local Ollama vision model** — run Ollama locally with a vision model
   pulled (default `gemma3:4b`; override via `ACARS_VISION_MODEL`). Assumes
   `OLLAMA_URL` (default `http://localhost:11434`) is reachable. Slow on
   CPU — `ACARS_VISION_TIMEOUT` defaults to 180s.
3. **Tesseract** (`pytesseract`) — always available as a last-resort
   fallback once the system binary is installed, but noticeably less
   accurate on FMS screen photos than either vision option.

At least one of these needs to actually work for the app to be useful.

### Optional: FlightAware cross-check

Set `FLIGHTAWARE_API_KEY` to enable cross-checking parsed times/route
against FlightAware. Skipped silently if unset.

### Optional: ScheduleMate/PFM route suggestions

`pfm_lookup.py` will try to read a PFM schedule database at
`../ScheduleMate/pfm.db` (or `PFM_DB_PATH`) to *suggest* dep/arr airports
for a tail/date. This is specific to the original author's employer
scheduling setup — if that file doesn't exist, lookups just return `None`
and the suggestion is silently skipped. Safe to ignore.

## Running

```bash
python3 app.py   # Flask on :5050
```

Flight records are stored locally in `instance/flightlog.db` (SQLite,
gitignored) in addition to being pushed to MyFlightBook — it's a local
staging/audit log, not the source of truth.
