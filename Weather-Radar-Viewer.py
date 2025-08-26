"""
Weather Radar Viewer
- Dual-location animated RainViewer radar over Google Static Maps
- NOAA hourly forecast + alerts
- Lightning strike overlay (community/unofficial)
Structure:
  1. Imports
  2. Color & style configuration
  3. Constants (API endpoints, timing)
  4. Utility helpers (projection, caches)
  5. Data fetchers (radar times, forecast, alerts, lightning)
  6. Overlay & image composition helpers
  7. RadarPanel class (per-location logic)
  8. RadarApp class (application shell / orchestration)
  9. Main entry
"""

# ===================== 1. IMPORTS =====================
import os, sys, io, math, time, threading, requests
from datetime import datetime, UTC
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None
import tkinter as tk
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from PIL import Image, ImageTk, ImageDraw
import json
from tkinter import simpledialog, messagebox
import re

# ===================== 2. COLOR & STYLE CONFIG =====================
COLORS = {
    "bg_root":        "#101020",
    "bg_panel":       "black",
    "bg_info":        "#101020",
    "bg_controls":    "#101020",
    "loading_fg":     "#ffcc00",
    "loading_bg":     "#101020",
    "center_bar":     "#CFCFDB",
    "gradient_top":   "#101020",
    "gradient_bottom":"#101060",
    "accent_cyan":    "cyan",
    "accent_yellow":  "#ffcc00",
    "accent_progress_bg": "#333",
    "accent_progress_border": "#666",
    "accent_progress_fill":  "#00ff99",
    "progress_knob_fill":    "#00ff99",
    "button_bg":      "#1a2340",
    "button_fg":      "white",
    "forecast_header_fg": "cyan",
    "forecast_grid_fg": "white",
    "forecast_legend_fg": "#888",
    "alert_fg":       "red",
    "pin_outer":      "white",
    "pin_inner":      "#ff2222",
    "lightning_fill": "yellow",
    "lightning_outline": "orange",
    "countdown_fg":   "cyan",
    "timestamp_fg":   "cyan",
    "separator":      "#2a3044"
}

FONT = {
    "title": ("Segoe UI", 16, "bold"),
    "timestamp": ("Segoe UI", 16, "bold"),
    "button": ("Segoe UI", 10),
    "loading": ("Segoe UI", 14),
    "panel_header": ("Arial", 14),
    "forecast_header": ("Segoe UI", 10, "bold"),
    "forecast_cell": ("Segoe UI", 9),
    "forecast_cell_bold": ("Segoe UI", 9, "bold"),
    "forecast_legend": ("Segoe UI", 8),
    "alert": ("Arial", 10, "bold"),
    "missing": ("Arial", 8),
    "countdown": ("Segoe UI", 12),
}

# --- add below FONT (after line defining FONT dict) ---
WEATHER_ICON_MAP = [
    ("thunder", "⛈"),
    ("t-storm", "⛈"),
    ("storm", "⛈"),
    ("snow", "❄"),
    ("sleet", "🌨"),
    ("ice", "🧊"),
    ("hail", "🧊"),
    ("rain", "🌧"),
    ("shower", "🌦"),
    ("drizzle", "🌦"),
    ("fog", "🌫"),
    ("mist", "🌫"),
    ("smoke", "🌫"),
    ("haze", "🌫"),
    ("cloudy", "☁"),
    ("overcast", "☁"),
    ("partly sunny", "🌤"),
    ("partly", "⛅"),
    ("sunny", "☀"),
    ("clear", "🌙"),
]

def get_weather_icon(desc: str) -> str:
    if not desc:
        return ""
    d = desc.lower()
    for key, icon in WEATHER_ICON_MAP:
        if key in d:
            return icon
    return "🔆"  # fallback generic

# ===================== 3. CONSTANTS & CONFIG =====================
RAINVIEWER_API = "https://api.rainviewer.com/public/weather-maps.json"
TILE_URL_TEMPLATE = "https://tilecache.rainviewer.com/v2/radar/{time}/256/{z}/{lat}/{lon}/2/1_1.png"
GOOGLE_MAPS_TEMPLATE = (
    "https://maps.googleapis.com/maps/api/staticmap?"
    "center={lat},{lon}&zoom={z}&size=256x256&maptype=roadmap&key={api_key}"
)
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
if not GOOGLE_MAPS_API_KEY:
    print("[Config] GOOGLE_MAPS_API_KEY not set. Map tiles may fail.")

GOOGLE_PLACES_AUTOCOMPLETE_URL = "https://maps.googleapis.com/maps/api/place/autocomplete/json"
GOOGLE_PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
_GOOGLE_AC_CACHE = {}          # input -> suggestions
_GOOGLE_PLACE_DETAILS_CACHE = {}  # place_id -> (name, lat, lon)

DEFAULT_LOCATIONS = [
    {"name": "Portland, OR", "lat": 45.5152, "lon": -122.6784},
    {"name": "Klamath Falls, OR", "lat": 42.2249, "lon": -121.7817}
]

INTEREST_PINS = []  # Removed location pins (was list of lat/lon tuples)

REFRESH_INTERVAL = 30000  # ms fallback radar refresh
DELAY_AFTER_FRAME_AVAILABLE_MS = 30000
ANIMATION_DELAY = 120
EXTENDED_MAX_FRAMES = 50
FORECAST_REFRESH_MS = 30 * 60 * 1000

ENABLE_LIGHTNING = True  # master toggle for lightning overlay
LIGHTNING_REFRESH_SECONDS = 60

FORECAST_PANEL_MIN_WIDTH = 230  # fixed width so map never overlaps forecast

# Add near other globals (after FORECAST_PANEL_MIN_WIDTH)
FAST_RESIZE_DEBOUNCE_MS = 180
FAST_RESIZE_MODE = False  # toggled during active window resizing

DEBUG_GEOCODE = False

# --- ADD near other globals (after autocomplete caches) ---
AUTOCOMPLETE_MIN_CHARS = 3
AUTOCOMPLETE_DEBOUNCE_MS = 350

# Stable base directory (support frozen exe)
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- PATCH: Persistent API key support (add AFTER BASE_DIR definition) ---
CONFIG_DIR = os.path.join(BASE_DIR, "radar_cache")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

def _load_saved_config():
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_config(data: dict):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[Config] Failed to save config: {e}")

_saved_cfg = _load_saved_config()
if _saved_cfg.get("api_key") and not GOOGLE_MAPS_API_KEY:
    GOOGLE_MAPS_API_KEY = _saved_cfg["api_key"]
    print("[Config] Loaded saved Google Maps API key.")

def google_places_autocomplete(partial: str, key: str):
    if not key or len(partial) < AUTOCOMPLETE_MIN_CHARS:
        return []
    k = partial.lower()
    cached = _GOOGLE_AC_CACHE.get(k)
    if cached:
        return cached
    try:
        params = {
            "input": partial,
            "types": "(cities)",
            "key": key
        }
        r = requests.get(GOOGLE_PLACES_AUTOCOMPLETE_URL, params=params, timeout=4)
        if not r.ok:
            return []
        data = r.json()
        preds = data.get("predictions", [])
        out = [(p.get("description",""), p.get("place_id","")) for p in preds[:6] if p.get("place_id")]
        _GOOGLE_AC_CACHE[k] = out
        return out
    except Exception:
        return []

def google_place_details(place_id: str, key: str):
    if place_id in _GOOGLE_PLACE_DETAILS_CACHE:
        return _GOOGLE_PLACE_DETAILS_CACHE[place_id]
    try:
        params = {
            "place_id": place_id,
            "fields": "name,geometry,formatted_address",
            "key": key
        }
        r = requests.get(GOOGLE_PLACES_DETAILS_URL, params=params, timeout=6)
        if not r.ok:
            return None
        data = r.json()
        res = data.get("result", {})
        geom = res.get("geometry", {}).get("location")
        if not geom:
            return None
        name = res.get("name") or res.get("formatted_address") or "Location"
        tup = (name, geom["lat"], geom["lng"])
        _GOOGLE_PLACE_DETAILS_CACHE[place_id] = tup
        return tup
    except Exception:
        return None

def geocode_city(name: str):
    """
    Geocode input. Supports raw 'lat,lon'.
    Order:
      1) Direct coords
      2) Google Places Details (if a synthetic place_id passed like 'place:XXXX')
      3) Fallback (Open-Meteo then Nominatim)
    """
    raw = (name or "").strip()
    if not raw:
        return None

    import re as _re
    m = _re.match(r'^\s*(-?\d+(\.\d+)?)\s*[,; ]\s*(-?\d+(\.\d+)?)\s*$', raw)
    if m:
        lat = float(m.group(1)); lon = float(m.group(3))
        return {"name": f"{lat:.3f},{lon:.3f}", "lat": lat, "lon": lon}

    # When user picks an autocomplete suggestion we encode as place:<place_id>
    if GOOGLE_MAPS_API_KEY and raw.startswith("place:"):
        place_id = raw.split(":", 1)[1]
        det = google_place_details(place_id, GOOGLE_MAPS_API_KEY)
        if det:
            nm, lat, lon = det
            return {"name": nm, "lat": lat, "lon": lon}

    # Use existing fallback path
    q = requests.utils.quote(raw)
    # (keep existing Open-Meteo + Nominatim logic below unchanged)
    try:
        url1 = f"https://geocoding-api.open-meteo.com/v1/search?name={q}&count=1&language=en&format=json"
        r1 = requests.get(url1, timeout=8)
        if r1.ok:
            data = r1.json()
            results = data.get("results") or []
            if results:
                top = results[0]
                disp = ", ".join(filter(None, [top.get("name"), top.get("admin1"), top.get("country")]))
                loc = {
                    "name": disp or raw,
                    "lat": top["latitude"],
                    "lon": top["longitude"]
                }
                if DEBUG_GEOCODE:
                    print(f"[Geocode] Open-Meteo hit: {loc}")
                return loc
            else:
                if DEBUG_GEOCODE:
                    print(f"[Geocode] Open-Meteo no results for '{raw}'")
        else:
            if DEBUG_GEOCODE:
                print(f"[Geocode] Open-Meteo HTTP {r1.status_code} for '{raw}'")
    except Exception as e:
        if DEBUG_GEOCODE:
            print(f"[Geocode] Open-Meteo error '{raw}': {e}")

    # 2) Fallback Nominatim (geocode.maps.co)
    try:
        url2 = f"https://geocode.maps.co/search?q={q}&limit=1"
        r2 = requests.get(url2, timeout=10, headers={"User-Agent": "RadarViewer/1.0"})
        if r2.ok:
            arr = r2.json()
            if isinstance(arr, list) and arr:
                top = arr[0]
                lat = float(top["lat"]); lon = float(top["lon"])
                disp = top.get("display_name", raw)
                loc = {"name": disp.split(",")[0], "lat": lat, "lon": lon}
                if DEBUG_GEOCODE:
                    print(f"[Geocode] Nominatim hit: {loc}")
                return loc
            else:
                if DEBUG_GEOCODE:
                    print(f"[Geocode] Nominatim no results for '{raw}'")
        else:
            if DEBUG_GEOCODE:
                print(f"[Geocode] Nominatim HTTP {r2.status_code} for '{raw}'")
    except Exception as e:
        if DEBUG_GEOCODE:
            print(f"[Geocode] Nominatim error '{raw}': {e}")

    return None

# --- REPLACE prompt_for_locations with Google autocomplete enabled ---
def prompt_for_locations(root, max_cities=2):
    """
    Popup for API key then locations.
    Adds Google Places city autocomplete after key applied.
    Selecting a suggestion stores 'place:<place_id>'.
    Includes checkbox to persist API key in radar_cache/config.json.
    """
    popup = tk.Toplevel(root)
    popup.title("Select Locations & API Key")
    popup.configure(bg=COLORS.get("bg_root", "#101020"))
    popup.grab_set(); popup.transient(root); popup.resizable(False, False)

    tk.Label(
        popup,
        text="1) Apply Google Maps API Key   2) Type a city (autocomplete) or lat,lon   3) OK",
        fg="white", bg=COLORS["bg_root"], font=("Segoe UI", 10, "bold"),
        wraplength=520, justify="left"
    ).pack(padx=16, pady=(14, 8))

    api_row = tk.Frame(popup, bg=COLORS["bg_root"]); api_row.pack(fill="x", padx=16, pady=(2,2))
    tk.Label(api_row, text="API Key:", fg="cyan", bg=COLORS["bg_root"]).pack(side="left")
    api_entry = tk.Entry(api_row, width=48, show="*"); api_entry.pack(side="left", padx=6)
    if GOOGLE_MAPS_API_KEY: api_entry.insert(0, GOOGLE_MAPS_API_KEY)
    apply_status = tk.Label(api_row, text="", fg="#ffcc00", bg=COLORS["bg_root"], font=("Segoe UI",8))
    apply_status.pack(side="left", padx=4)
    api_applied = {"ok": bool(GOOGLE_MAPS_API_KEY)}
    save_var = tk.BooleanVar(value=bool(_saved_cfg.get("api_key")))
    save_frame = tk.Frame(popup, bg=COLORS["bg_root"]); save_frame.pack(fill="x", padx=16, pady=(0,4))
    tk.Checkbutton(save_frame, text="Save API key for next startup",
                   variable=save_var, fg="white", bg=COLORS["bg_root"],
                   activebackground=COLORS["bg_root"], selectcolor="#202830").pack(anchor="w")

    def persist_api_key():
        if save_var.get() and GOOGLE_MAPS_API_KEY:
            cfg = _load_saved_config()
            cfg["api_key"] = GOOGLE_MAPS_API_KEY
            _save_config(cfg)
            print("[Config] API key saved.")
        elif not save_var.get():
            # If unchecked, remove saved key but keep other entries
            cfg = _load_saved_config()
            if "api_key" in cfg:
                cfg.pop("api_key", None)
                _save_config(cfg)
                print("[Config] Saved API key removed.")

    def apply_key():
        val = api_entry.get().strip()
        if not val:
            apply_status.config(text="(empty)")
            return
        global GOOGLE_MAPS_API_KEY
        GOOGLE_MAPS_API_KEY = val
        api_applied["ok"] = True
        apply_status.config(text="Applied ✓", fg="#00dd88")
        persist_api_key()
        print(f"[Config] GOOGLE_MAPS_API_KEY set (len={len(val)})")

    tk.Button(popup, text="Apply Key", command=apply_key,
              bg=COLORS["button_bg"], fg="white", relief="flat", width=12).pack(padx=16, anchor="w")

    tk.Label(popup, text="Locations (city autocomplete after key) or raw lat,lon:",
             fg="#ffcc00", bg=COLORS["bg_root"], font=("Segoe UI",9,"bold")).pack(fill="x", padx=16, pady=(6,2))

    entry_rows = []
    suggestion_boxes = []
    _ac_state = {"jobs": {}, "threads": {}, "last_query": {}}

    def destroy_suggestions():
        for lb in list(suggestion_boxes):
            try:
                if lb.winfo_exists(): lb.destroy()
            except Exception: pass
        suggestion_boxes.clear()

    def show_suggestions(entry_widget, idx, suggestions):
        destroy_suggestions()
        if not suggestions: return
        lb = tk.Listbox(popup, height=min(6, len(suggestions)), bg="#182030", fg="white",
                        activestyle="dotbox", exportselection=False)
        lb.place(in_=entry_widget, relx=0, rely=1, x=0, y=0, anchor="nw")
        for d, pid in suggestions: lb.insert("end", d)
        suggestion_boxes.append(lb)
        def pick(evt=None):
            sel = lb.curselection()
            if not sel: return
            desc, place_id = suggestions[sel[0]]
            entry_widget.delete(0,"end"); entry_widget.insert(0, f"place:{place_id}")
            try: lb.destroy()
            except Exception: pass
            validate_field(entry_widget, entry_rows[idx][1])
        lb.bind("<Double-1>", pick); lb.bind("<Return>", pick); lb.bind("<Escape>", lambda e: destroy_suggestions())

    def schedule_autocomplete(entry_widget, idx):
        aid = _ac_state["jobs"].get(entry_widget)
        if aid:
            try: popup.after_cancel(aid)
            except Exception: pass
        _ac_state["jobs"][entry_widget] = popup.after(AUTOCOMPLETE_DEBOUNCE_MS,
                                                      lambda: run_autocomplete(entry_widget, idx))

    def run_autocomplete(entry_widget, idx):
        txt = entry_widget.get().strip()
        if (not api_applied["ok"]) or txt.startswith("place:") or "," in txt or len(txt) < AUTOCOMPLETE_MIN_CHARS:
            destroy_suggestions(); return
        if _ac_state["last_query"].get(entry_widget) == txt: return
        _ac_state["last_query"][entry_widget] = txt
        def worker():
            suggestions = google_places_autocomplete(txt, GOOGLE_MAPS_API_KEY)
            try:
                popup.after(0, lambda: show_suggestions(entry_widget, idx,
                            suggestions if entry_widget.get().strip() == txt else []))
            except Exception: pass
        _ac_state["threads"][entry_widget] = threading.Thread(target=worker, daemon=True); _ac_state["threads"][entry_widget].start()

    def validate_field(entry_widget, status_widget):
        txt = entry_widget.get().strip()
        if not txt:
            status_widget.config(text=""); return
        # place id chosen
        if txt.startswith("place:") and api_applied["ok"]:
            status_widget.config(text="✓", fg="#00dd88"); return
        # coords
        import re as _re
        if _re.match(r'^\s*-?\d+(\.\d+)?\s*[, ]\s*-?\d+(\.\d+)?\s*$', txt):
            status_widget.config(text="✓", fg="#00dd88"); return
        if not api_applied["ok"]:
            status_widget.config(text="(Apply key)", fg="#ff6666"); return
        loc = geocode_city(txt)
        status_widget.config(text="✓" if loc else "?", fg="#00dd88" if loc else "#ff6666")

    for i in range(max_cities):
        row = tk.Frame(popup, bg=COLORS["bg_root"]); row.pack(fill="x", padx=16, pady=4)
        tk.Label(row, text=f"Location {i+1}:", fg="cyan", bg=COLORS["bg_root"]).pack(side="left")
        e = tk.Entry(row, width=42); e.pack(side="left", padx=6)
        st = tk.Label(row, text="", fg="#888", bg=COLORS["bg_root"], font=("Segoe UI",8)); st.pack(side="left")
        entry_rows.append((e, st))
        def on_key(ev, ent=e, stat=st, idx=i):
            validate_field(ent, stat); destroy_suggestions(); schedule_autocomplete(ent, idx)
        e.bind("<KeyRelease>", on_key)
        e.bind("<FocusOut>", lambda ev, ent=e, stat=st: validate_field(ent, stat))

    hint = tk.Label(popup, text="Example: 'San Francisco' -> pick list, or '45.52,-122.68'.",
                    fg="#888", bg=COLORS["bg_root"], font=("Segoe UI",8))
    hint.pack(fill="x", padx=16, pady=(0,4))

    status_label = tk.Label(popup, text="Apply key first (if entering one).",
                            fg="#ffcc00", bg=COLORS["bg_root"], font=("Segoe UI",9), anchor="w")
    status_label.pack(fill="x", padx=16, pady=(2,2))

    result = {"locations": None}

    def resolve_entry(txt):
        if not txt: return None
        if txt.startswith("place:") and api_applied["ok"]:
            det = google_place_details(txt.split(":",1)[1], GOOGLE_MAPS_API_KEY)
            if det:
                nm, lat, lon = det; return {"name": nm, "lat": lat, "lon": lon}
        return geocode_city(txt)

    def submit():
        if api_entry.get().strip() and not api_applied["ok"]:
            status_label.config(text="Click 'Apply Key' first."); return
        names = [e.get().strip() for e,_ in entry_rows if e.get().strip()]
        resolved = []
        for n in names:
            loc = resolve_entry(n)
            if loc: resolved.append(loc)
            else: status_label.config(text=f"Could not resolve '{n}' (skipped).")
        if not resolved:
            resolved = DEFAULT_LOCATIONS[:max_cities]
            status_label.config(text="Using defaults.")
        if api_applied["ok"]:
            persist_api_key()
        result["locations"] = resolved
        destroy_suggestions()
        popup.destroy()

    def use_defaults():
        if api_applied["ok"]:
            persist_api_key()
        result["locations"] = DEFAULT_LOCATIONS[:max_cities]
        destroy_suggestions()
        popup.destroy()

    btn_frame = tk.Frame(popup, bg=COLORS["bg_root"]); btn_frame.pack(fill="x", padx=16, pady=12)
    tk.Button(btn_frame, text="OK", width=10, command=submit,
              bg=COLORS["button_bg"], fg="white", relief="flat").pack(side="left")
    tk.Button(btn_frame, text="Defaults", width=10, command=use_defaults,
              bg="#303a55", fg="white", relief="flat").pack(side="left", padx=8)
    tk.Button(btn_frame, text="Cancel", width=10, command=use_defaults,
              bg="#553030", fg="white", relief="flat").pack(side="right")

    popup.bind("<Return>", lambda e: submit()); popup.bind("<Escape>", lambda e: use_defaults())
    root.update_idletasks()
    w, h = 600, 500
    x = root.winfo_rootx() + max(0,(root.winfo_width()-w)//2); y = root.winfo_rooty() + max(0,(root.winfo_height()-h)//2)
    popup.geometry(f"{w}x{h}+{x}+{y}")
    entry_rows[0][0].focus_set()
    root.wait_window(popup)
    return result["locations"] or DEFAULT_LOCATIONS[:max_cities]

# ===================== 4. UTILITY HELPERS =====================
def world_px(lat, lon, z):
    """Convert lat/lon to 'world pixel' coordinates at zoom z (Web Mercator)."""
    scale = 256 * (2 ** z)
    x = (lon + 180.0) / 360.0 * scale
    siny = math.sin(math.radians(lat))
    siny = min(max(siny, -0.9999), 0.9999)
    y = (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * scale
    return x, y

# Forecast cache
_forecast_cache = {}
_forecast_cache_time = {}
requests_session = requests.Session()

_last_lightning_fetch_time = 0
_last_lightning_data = []
_lightning_disabled = False

# --- PATCH 1: Add a lock to prevent multiple simultaneous lightning fetches (top near other globals) ---
lightning_lock = threading.Lock()

# ===================== 5. DATA FETCHERS =====================
def get_latest_radar_times():
    try:
        r = requests.get(RAINVIEWER_API, timeout=10)
        r.raise_for_status()
        data = r.json()
        return [str(t["time"]) for t in data["radar"]["past"]]
    except Exception as e:
        print(f"Error fetching RainViewer radar times: {e}")
        return []

# --- PATCH 2: Update fetch_lightning_strikes to use the lock (replace existing definition) ---
def fetch_lightning_strikes():
    """Return recent lightning strikes (cached). Auto-disables after 404 to stop spam."""
    global _last_lightning_fetch_time, _last_lightning_data, _lightning_disabled
    if not ENABLE_LIGHTNING or _lightning_disabled:
        return []
    now = time.time()
    # Serve cached quickly
    if now - _last_lightning_fetch_time < LIGHTNING_REFRESH_SECONDS and _last_lightning_data:
        return _last_lightning_data
    with lightning_lock:
        # Re-check inside lock
        if now - _last_lightning_fetch_time < LIGHTNING_REFRESH_SECONDS and _last_lightning_data:
            return _last_lightning_data
        url = "https://www.lightningmaps.org/live/geojson.php"
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 404:
                _lightning_disabled = True
                print("[Lightning] Endpoint returned 404 once. Disabling lightning overlay for this session.")
                return []
            r.raise_for_status()
            data = r.json()
            strikes = []
            for feat in data.get("features", []):
                coords = feat.get("geometry", {}).get("coordinates")
                if coords and len(coords) == 2:
                    lon, lat = coords
                    strikes.append((lat, lon))
            _last_lightning_fetch_time = now
            _last_lightning_data = strikes
            return strikes
        except Exception as e:
            if now - _last_lightning_fetch_time > 5:
                print(f"[Lightning] Fetch error: {e}")
            _last_lightning_fetch_time = now
            return []

def get_hourly_forecast(lat, lon):
    key = (lat, lon)
    if key in _forecast_cache:
        ts = _forecast_cache_time.get(key, 0)
        if (time.time() - ts) < (FORECAST_REFRESH_MS / 1000.0):
            return _forecast_cache[key]
    try:
        url_points = f"https://api.weather.gov/points/{lat},{lon}"
        r_points = requests_session.get(url_points, timeout=10, headers={"User-Agent": "RadarViewer/1.0"})
        r_points.raise_for_status()
        data_points = r_points.json()
        forecast_hourly_url = data_points["properties"]["forecastHourly"]
        tz_name = data_points["properties"].get("timeZone", "UTC")

        r_forecast = requests_session.get(forecast_hourly_url, timeout=10, headers={"User-Agent": "RadarViewer/1.0"})
        r_forecast.raise_for_status()
        periods = r_forecast.json()["properties"]["periods"][:12]

        entries = []
        for p in periods:
            try:
                dt_obj = datetime.fromisoformat(p["startTime"].replace("Z", "+00:00"))
                if ZoneInfo and tz_name:
                    try:
                        dt_obj = dt_obj.astimezone(ZoneInfo(tz_name))
                    except Exception:
                        pass
            except Exception:
                dt_obj = p["startTime"]
            temp = p.get("temperature")
            if temp is not None and p.get("temperatureUnit") == "F":
                temp = (temp - 32) * 5.0 / 9.0
            weather = p.get("shortForecast", "Unknown")
            pop = p.get("probabilityOfPrecipitation", {}).get("value")

            wind_speed = None
            ws_txt = p.get("windSpeed", "")
            if ws_txt:
                try:
                    wind_speed = float(ws_txt.split()[0]) * 0.44704  # mph->m/s
                except Exception:
                    pass
            compass_map = {
                "N": 0, "NNE": 22, "NE": 45, "ENE": 67, "E": 90, "ESE": 112, "SE": 135, "SSE": 157,
                "S": 180, "SSW": 202, "SW": 225, "WSW": 247, "W": 270, "WNW": 292, "NW": 315, "NNW": 337
            }
            wind_dir = compass_map.get(p.get("windDirection"), None)
            entries.append({
                "time": dt_obj,
                "temp": temp,
                "weather": weather,
                "pop": pop,
                "wind_speed": wind_speed,
                "wind_dir": wind_dir
            })
        result = {"timezone": tz_name, "entries": entries}
        _forecast_cache[key] = result
        _forecast_cache_time[key] = time.time()
        return result
    except Exception as e:
        print(f"Error fetching NOAA forecast for {lat},{lon}: {e}")
        return {"timezone": "UTC", "entries": []}

# ===================== 6. OVERLAY & IMAGE HELPERS =====================
def overlay_lightning_strikes(img, zoom, strikes, center_lat, center_lon):
    draw = ImageDraw.Draw(img)
    width, height = img.size
    cx, cy = world_px(center_lat, center_lon, zoom)
    for lat, lon in strikes:
        px, py = world_px(lat, lon, zoom)
        ix = int(round(width / 2 + (px - cx)))
        iy = int(round(height / 2 + (py - cy)))
        if 0 <= ix < width and 0 <= iy < height:
            r = 4
            draw.ellipse(
                (ix - r, iy - r, ix + r, iy + r),
                fill=COLORS["lightning_fill"],
                outline=COLORS["lightning_outline"]
            )
    return img

def add_interest_pins(img, pins, zoom, center_lat, center_lon):
    if not img or getattr(img, "_pins_added", False):
        return
    w, h = img.size
    if not w or not h:
        return
    cx, cy = world_px(center_lat, center_lon, zoom)
    d = ImageDraw.Draw(img)
    for plat, plon in pins:
        px, py = world_px(plat, plon, zoom)
        ix = int(round(w / 2 + (px - cx)))
        iy = int(round(h / 2 + (py - cy)))
        if -5 <= ix <= w + 5 and -5 <= iy <= h + 5:
            r_outer, r_inner = 5, 3
            d.ellipse((ix - r_outer, iy - r_outer, ix + r_outer, iy + r_outer), fill=COLORS["pin_outer"])
            d.ellipse((ix - r_inner, iy - r_inner, ix + r_inner, iy + r_inner), fill=COLORS["pin_inner"])
    setattr(img, "_pins_added", True)

# ===================== 7. PER-LOCATION PANEL =====================
class RadarPanel:
    def __init__(self, parent, location, radar_times, loading_callback, get_size_callback):
        self.location = location
        self.radar_times = radar_times
        self.loading_callback = loading_callback
        self.get_size_callback = get_size_callback

        self.panel_frame = tk.Frame(parent, bg=COLORS["bg_panel"])
        self.panel_frame.pack(side="left", fill="both", expand=True)

        self.label = tk.Label(self.panel_frame, bg=COLORS["bg_panel"], fg="white",
                              text=location["name"], font=FONT["panel_header"])
        self.label.pack(fill="x")

        self.refresh_btn = tk.Button(
            self.panel_frame, text="↻ Forecast", command=self.manual_refresh_forecast,
            font=FONT["button"], bg="#203050", fg="white", relief="flat"
        )
        self.refresh_btn.pack(fill="x", padx=2, pady=(0, 4))

        # Alerts (now rendered inside the forecast info panel, below the forecast grid)
        self.show_alerts_var = tk.BooleanVar(value=False)
        self.alerts_checkbox = None
        self.alerts_container = None  # will be a Frame inside info_frame when created

        # New body frame with grid to prevent overlap
        self.body_frame = tk.Frame(self.panel_frame, bg=COLORS["bg_panel"])
        self.body_frame.pack(fill="both", expand=True)

        self.info_frame = tk.Frame(self.body_frame, bg=COLORS["bg_info"], width=FORECAST_PANEL_MIN_WIDTH)
        self.info_frame.grid(row=0, column=0, sticky="ns")
        # Prevent grid from shrinking below set width
        self.info_frame.grid_propagate(False)

        self.composite_image_label = tk.Label(self.body_frame, bg=COLORS["bg_panel"])
        self.composite_image_label.grid(row=0, column=1, sticky="nsew")

        self.body_frame.grid_columnconfigure(0, weight=0, minsize=FORECAST_PANEL_MIN_WIDTH)
        self.body_frame.grid_columnconfigure(1, weight=1)
        self.body_frame.grid_rowconfigure(0, weight=1)

        self.composite_images_pil = []
        # self.composite_images_tk = []  # no longer pre-building static sized images
        self.frame_index = 0
        self.map_image_pil = None
        self._scaled_cache = {}   # (frame_index, w, h) -> PhotoImage
        self._last_size = (0, 0)

        self._forecast_after_id = None

        self.load_map()
        # Re-render on size changes
        self.composite_image_label.bind("<Configure>", lambda e: self.show_frame(self.frame_index))
        self.panel_frame.after(600, self.update_forecast)
        self.panel_frame.after(800, self.check_alerts_and_update_ui)

    # ---- Alerts ----
    def get_noaa_alerts(self):
        lat = self.location["lat"]; lon = self.location["lon"]
        url = f"https://api.weather.gov/alerts/active?point={lat},{lon}"
        try:
            r = requests_session.get(url, timeout=10, headers={"User-Agent": "RadarViewer/1.0"})
            r.raise_for_status()
            features = r.json().get("features", [])
            out = []
            for f in features:
                props = f.get("properties", {})
                headline = props.get("headline")
                event = props.get("event")
                desc = props.get("description")
                if headline: out.append(headline)
                elif event: out.append(event)
                elif desc: out.append(desc[:60] + "..." if len(desc) > 60 else desc)
            return out
        except Exception:
            return []

    def check_alerts_and_update_ui(self):
        """Fetch alerts and (re)build the alerts UI section at the bottom of the forecast panel."""
        alerts = self.get_noaa_alerts()

        # If forecast panel was redrawn, previous checkbox/container may be gone
        # Ensure checkbox built just above (before) alerts list (if any)
        if self.alerts_checkbox and not self.alerts_checkbox.winfo_exists():
            self.alerts_checkbox = None
        if self.alerts_container and not self.alerts_container.winfo_exists():
            self.alerts_container = None

        # Rebuild checkbox
        if self.alerts_checkbox:
            self.alerts_checkbox.destroy()
            self.alerts_checkbox = None
        if alerts:
            self.alerts_checkbox = tk.Checkbutton(
                self.info_frame,
                text="Show Alerts",
                variable=self.show_alerts_var,
                command=self.toggle_alerts,
                bg=COLORS["bg_info"],
                fg=COLORS["alert_fg"],
                selectcolor="#222",
                font=FONT["alert"],
                anchor="w",
                padx=4
            )
            self.alerts_checkbox.pack(fill="x", pady=(8, 0))

        # Build or remove alert list
        if self.show_alerts_var.get() and alerts:
            self.update_alerts(alerts)
        else:
            if self.alerts_container and self.alerts_container.winfo_exists():
                self.alerts_container.destroy()
                self.alerts_container = None

    def toggle_alerts(self):
        self.check_alerts_and_update_ui()

    def update_alerts(self, alerts):
        if self.alerts_container and self.alerts_container.winfo_exists():
            self.alerts_container.destroy()
        self.alerts_container = tk.Frame(self.info_frame, bg=COLORS["bg_info"])
        self.alerts_container.pack(fill="x", pady=(2, 6))
        for alert in alerts:
            tk.Label(
                self.alerts_container,
                text=alert,
                fg=COLORS["alert_fg"],
                bg=COLORS["bg_info"],
                font=FONT["alert"],
                wraplength=FORECAST_PANEL_MIN_WIDTH - 10,
                justify="left",
                anchor="w",
                padx=4
            ).pack(fill="x", anchor="w", pady=1)

    # ---- Map & Radar ----
    def load_map(self):
        z = 8
        map_url = GOOGLE_MAPS_TEMPLATE.format(
            lat=self.location["lat"], lon=self.location["lon"], z=z, api_key=GOOGLE_MAPS_API_KEY
        )
        try:
            r_map = requests_session.get(map_url, timeout=10, headers={"User-Agent": "RadarViewer/1.0"})
            r_map.raise_for_status()
            self.map_image_pil = Image.open(io.BytesIO(r_map.content)).convert("RGBA")
            # Removed add_interest_pins call
        except Exception as e:
            print(f"Error loading Google map for {self.location['name']}: {e}")
            self.map_image_pil = None

    def load_radar_frames(self):
        self.composite_images_pil.clear()
        if not self.map_image_pil:
            return
        z = 8
        headers = {"User-Agent": "Mozilla/5.0"}
        safe_loc = ''.join(c if c.isalnum() else '_' for c in self.location['name']).lower()
        cache_dir = os.path.join(BASE_DIR, "radar_cache", safe_loc)
        os.makedirs(cache_dir, exist_ok=True)
        print(f"[Cache] Using cache directory for {self.location['name']}: {cache_dir}")

        lightning_strikes = fetch_lightning_strikes()

        def build_filename(ts: str) -> str:
            # ts is epoch string
            try:
                dt = datetime.fromtimestamp(int(ts), UTC)
                human = dt.strftime("%Y-%m-%d_%H%MUTC")
                return f"{int(ts)}_{human}.png"
            except Exception:
                return f"{ts}.png"

        def cache_path(ts: str):
            return os.path.join(cache_dir, build_filename(ts))

        def find_existing_for_ts(ts: str):
            # Accept new or legacy naming (legacy: ts.png)
            prefix = f"{ts}"
            for fname in os.listdir(cache_dir):
                if fname.startswith(prefix) and fname.endswith(".png"):
                    return os.path.join(cache_dir, fname)
            legacy = os.path.join(cache_dir, f"{ts}.png")
            return legacy if os.path.exists(legacy) else None

        def load_or_fetch(ts):
            existing = find_existing_for_ts(ts)
            if existing and os.path.exists(existing):
                try:
                    cached = Image.open(existing).convert("RGBA")
                    # Removed add_interest_pins
                    overlay_lightning_strikes(cached, z, lightning_strikes,
                                              self.location['lat'], self.location['lon'])
                    return cached
                except Exception:
                    pass
            radar_url = TILE_URL_TEMPLATE.format(time=ts, z=z, lat=self.location['lat'], lon=self.location['lon'])
            try:
                r_radar = requests_session.get(radar_url, timeout=10, headers=headers)
                r_radar.raise_for_status()
                radar_img = Image.open(io.BytesIO(r_radar.content)).convert("RGBA")
                alpha = radar_img.split()[-1].point(lambda p: int(p * 0.7))
                radar_img.putalpha(alpha)

                base = self.map_image_pil.copy()
                # Removed add_interest_pins
                overlay_lightning_strikes(base, z, lightning_strikes, self.location['lat'], self.location['lon'])
                composite = Image.alpha_composite(base, radar_img)
                try:
                    out_path = cache_path(ts)
                    composite.save(out_path, "PNG")
                    print(f"[Cache] Saved radar frame {os.path.basename(out_path)} for {self.location['name']}")
                except Exception as e:
                    print(f"Cache save failed ({ts}): {e}")
                return composite
            except Exception as e:
                if existing and os.path.exists(existing):
                    try:
                        return Image.open(existing).convert("RGBA")
                    except Exception:
                        pass
                print(f"Radar fetch failed for {self.location['name']} {ts}: {e}")
                img = self.map_image_pil.copy()
                d = ImageDraw.Draw(img)
                w, h = img.size
                d.line((0, 0, w, h), fill=(255, 0, 0, 80), width=6)
                d.line((0, h, w, 0), fill=(255, 0, 0, 80), width=6)
                return img

        total_count = len(self.radar_times)
        for idx, ts in enumerate(self.radar_times):
            img = load_or_fetch(ts)
            # Removed add_interest_pins
            self.composite_images_pil.append(img)
            self.loading_callback(self.location['name'], idx + 1, total_count)

        # Trim cache keeping newest EXTENDED_MAX_FRAMES by epoch prefix
        try:
            files = [f for f in os.listdir(cache_dir) if f.endswith(".png")]
            parsed = []
            for f in files:
                m = re.match(r'^(\d+)', f)  # leading epoch digits
                if m:
                    try:
                        parsed.append((int(m.group(1)), f))
                    except Exception:
                        pass
            parsed.sort()
            if len(parsed) > EXTENDED_MAX_FRAMES:
                to_delete = parsed[0:len(parsed)-EXTENDED_MAX_FRAMES]
                for epoch, fname in to_delete:
                    try:
                        os.remove(os.path.join(cache_dir, fname))
                    except Exception:
                        pass
        except Exception as e:
            print(f"Cache trim error for {self.location['name']}: {e}")

        self.panel_frame.after(0, lambda: self.show_frame(self.frame_index))

    def update_scaled_images(self):
        # Deprecated: dynamic scaling handled in show_frame
        return

    # --- Modify RadarPanel.show_frame (replace existing method body) ---
    def show_frame(self, frame_index):
        if not self.composite_images_pil:
            return
        frame_index = min(frame_index, len(self.composite_images_pil) - 1)
        base_img = self.composite_images_pil[frame_index]

        avail_w = self.composite_image_label.winfo_width()
        avail_h = self.composite_image_label.winfo_height()
        if avail_w <= 2 or avail_h <= 2:
            self.panel_frame.after(50, lambda: self.show_frame(frame_index))
            return

        bw, bh = base_img.size
        scale = min(avail_w / bw, avail_h / bh)
        if scale <= 0:
            return
        new_w = max(1, int(bw * scale))
        new_h = max(1, int(bh * scale))

        # Choose faster algorithm during active resize to stay smooth
        from PIL import Image as _PILImage
        global FAST_RESIZE_MODE
        resample_mode = _PILImage.NEAREST if FAST_RESIZE_MODE else _PILImage.LANCZOS

        key = (frame_index, new_w, new_h, resample_mode)
        if key in self._scaled_cache:
            tk_img = self._scaled_cache[key]
        else:
            resized = base_img.resize((new_w, new_h), resample_mode)
            tk_img = ImageTk.PhotoImage(resized)
            if len(self._scaled_cache) > 60:
                # Drop older cached sizes (keep only current size family)
                self._scaled_cache = {k: v for k, v in self._scaled_cache.items() if k[1:] == key[1:]}
            self._scaled_cache[key] = tk_img

        self.composite_image_label.config(image=tk_img)
        self.composite_image_label.image = tk_img
        self._last_frame = frame_index

        # Missing indicator (unchanged logic)
        if hasattr(self, 'missing_label') and self.missing_label.winfo_exists():
            self.missing_label.destroy()
        try:
            px = base_img.getpixel((10, 10))
            if px[0] > 200 and px[1] < 100 and px[2] < 100 and (len(px) < 4 or px[3] > 50):
                self.missing_label = tk.Label(
                    self.panel_frame,
                    text="No radar image for this time (red X).",
                    fg="#ff6666",
                    bg=COLORS["bg_panel"],
                    font=FONT["missing"],
                    wraplength=220, justify="center"
                )
                self.missing_label.pack(side="bottom", fill="x", pady=2)
        except Exception:
            pass

    # ---- Forecast ----
    def update_forecast(self):
        data = get_hourly_forecast(self.location["lat"], self.location["lon"])
        tz_name = data.get("timezone", "")
        entries = data.get("entries", [])

        for wdg in self.info_frame.winfo_children(): wdg.destroy()
        if not entries:
            tk.Label(self.info_frame, text="Forecast unavailable",
                     fg="white", bg=COLORS["bg_info"], font=FONT["forecast_header"]).pack(anchor="w")
            return

        try:
            first_dt = entries[0]["time"]
            if hasattr(first_dt, "strftime"):
                tz_abbr = first_dt.strftime("%Z") or tz_name.split("/")[-1]
            else:
                tz_abbr = tz_name.split("/")[-1]
        except Exception:
            tz_abbr = tz_name.split("/")[-1] if tz_name else ""

        tk.Label(self.info_frame, text=f"Next 12 hrs ({tz_abbr})",
                 fg=COLORS["forecast_header_fg"], bg=COLORS["bg_info"],
                 font=FONT["forecast_header"]).pack(anchor="w")

        columns = ["Hour", "Wx", "Temp", "Precip", "Wind"]
        grid_frame = tk.Frame(self.info_frame, bg=COLORS["bg_info"])
        grid_frame.pack(fill="x")

        for col, name in enumerate(columns):
            tk.Label(grid_frame, text=name, fg=COLORS["forecast_header_fg"],
                     bg=COLORS["bg_info"], font=FONT["forecast_cell_bold"],
                     borderwidth=1, relief="ridge", padx=2, pady=2).grid(row=0, column=col, sticky="nsew")

        arrows = ["↑","↗","→","↘","↓","↙","←","↖"]
        for i, e in enumerate(entries):
            dt_obj = e["time"]
            hour_str = dt_obj.strftime("%a %I%p").lstrip("0") if hasattr(dt_obj, "strftime") else str(dt_obj)
            temp_txt = f"{e['temp']:.0f}°C" if e['temp'] is not None else ""
            weather_desc = e["weather"] or ""
            icon = get_weather_icon(weather_desc)
            pop_txt = f"{int(e.get('pop') or 0)}%"
            wind_txt = ""
            wd = e.get("wind_dir")
            ws = e.get("wind_speed")
            if wd is not None and ws is not None:
                idx = int(((int(wd) + 22.5) % 360) // 45)
                wind_txt = f"{arrows[idx]} {ws:.1f}m/s"
            row_vals = [hour_str, icon, temp_txt, pop_txt, wind_txt]
            for c, val in enumerate(row_vals):
                tk.Label(grid_frame, text=val, fg=COLORS["forecast_grid_fg"], bg=COLORS["bg_info"],
                         font=FONT["forecast_cell"], borderwidth=1, relief="groove",
                         padx=2, pady=2).grid(row=i+1, column=c, sticky="nsew")

        try:
            start_dt = entries[0]["time"]; end_dt = entries[-1]["time"]
            legend_text = (f"Covers {start_dt.strftime('%a %I:%M %p').lstrip('0')} to "
                           f"{end_dt.strftime('%I:%M %p').lstrip('0')}") if hasattr(start_dt, "strftime") else f"Starting {start_dt}"
            tk.Label(self.info_frame, text=legend_text, fg=COLORS["forecast_legend_fg"],
                     bg=COLORS["bg_info"], font=FONT["forecast_legend"]).pack(anchor="w", pady=(4,0))
        except Exception:
            pass

        temps = [e["temp"] for e in entries]
        pops = [ (e.get("pop") or 0) for e in entries]
        hours = [ (e["time"].strftime("%I%p").lstrip("0") if hasattr(e["time"], "strftime") else str(e["time"])) for e in entries]
        xvals = list(range(len(hours)))

        if hasattr(self, "graph_canvas"):
            try:
                if self.graph_canvas:
                    self.graph_canvas.get_tk_widget().destroy()
                if hasattr(self, "graph_figure") and self.graph_figure:
                    plt.close(self.graph_figure)
            except Exception:
                pass

        fig, ax1 = plt.subplots(figsize=(3, 2.0), dpi=100)
        ax1.plot(xvals, temps, color="tab:red", marker="o")
        ax1.set_ylabel("Temp (°C)", color="tab:red")
        ax1.tick_params(axis="y", labelcolor="tab:red")
        ax1.set_xticks(xvals)
        ax1.set_xticklabels(hours, rotation=45, fontsize=8)
        ax1.set_ylim(bottom=0)
        ax1.set_xlim(-0.2, len(xvals) - 0.8)
        ax2 = ax1.twinx()
        ax2.plot(xvals, pops, color="tab:blue", marker="x", linestyle="--")
        ax2.set_ylabel("Precip (%)", color="tab:blue")
        ax2.tick_params(axis="y", labelcolor="tab:blue")
        ax2.set_ylim(0, 100)
        fig.tight_layout()
        canvas = FigureCanvasTkAgg(fig, master=self.info_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(side="bottom", fill="x", pady=6)
        self.graph_canvas = canvas
        self.graph_figure = fig

        # Rebuild alerts section below the forecast each refresh
        self.check_alerts_and_update_ui()

        if self._forecast_after_id:
            try: self.info_frame.after_cancel(self._forecast_after_id)
            except Exception: pass
        self._forecast_after_id = self.info_frame.after(FORECAST_REFRESH_MS, self.update_forecast)

    def manual_refresh_forecast(self):
        key = (self.location["lat"], self.location["lon"])
        _forecast_cache.pop(key, None)
        _forecast_cache_time.pop(key, None)
        self.update_forecast()

# ===================== 8. APPLICATION SHELL =====================
class RadarApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Oregon Radar Viewer")
        self.root.geometry("900x400")
        self.closing = False

        # Initialize gradient / resize state BEFORE first draw_gradient call
        self._gradient_size = (0, 0)
        self._gradient_photo = None
        self._resize_idle_after_id = None

        # Background gradient canvas
        self.bg_canvas = tk.Canvas(self.root, highlightthickness=0, bd=0)
        self.bg_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.draw_gradient()  # safe now (attributes exist)

        # MAIN VERTICAL LAYOUT CONTAINER
        self.main_col = tk.Frame(self.root, bg=COLORS["bg_root"])
        self.main_col.pack(fill="both", expand=True)

        # --- HEADER BLOCK (timestamp + controls) ---
        self.header_frame = tk.Frame(self.main_col, bg=COLORS["bg_controls"])
        self.header_frame.pack(side="top", fill="x", pady=(4, 0))

        self.timestamp_label = tk.Label(self.header_frame, text="",
                                        font=FONT["timestamp"],
                                        bg=COLORS["bg_controls"],
                                        fg=COLORS["timestamp_fg"])
        self.timestamp_label.pack(side="top", fill="x")

        self.controls_frame = tk.Frame(self.header_frame, bg=COLORS["bg_controls"])
        self.controls_frame.pack(side="top", fill="x", pady=(4, 4))

        self.is_paused = False  # allow auto-play
        self.pause_btn = tk.Button(self.controls_frame, text="⏸ Pause",
                                   font=FONT["button"], command=self.toggle_pause,
                                   bg=COLORS["button_bg"], fg=COLORS["button_fg"], relief="flat")
        self.pause_btn.pack(side="left", padx=6, pady=2)

        self.prev_btn = tk.Button(self.controls_frame, text="−", font=("Segoe UI", 12, "bold"),
                                  width=2, command=self.step_prev,
                                  bg=COLORS["button_bg"], fg="white", relief="flat")
        self.prev_btn.pack(side="left", padx=2)

        self.next_btn = tk.Button(self.controls_frame, text="+", font=("Segoe UI", 12, "bold"),
                                  width=2, command=self.step_next,
                                  bg=COLORS["button_bg"], fg="white", relief="flat")
        self.next_btn.pack(side="left", padx=2)

        # Horizontal separator
        tk.Frame(self.main_col, height=2, bg=COLORS["separator"]).pack(fill="x", pady=(0, 2))

        # --- STATUS / PROGRESS BLOCK ---
        self.status_frame = tk.Frame(self.main_col, bg=COLORS["bg_controls"])
        self.status_frame.pack(side="top", fill="x", pady=(0, 4))

        self.progress_canvas = tk.Canvas(self.status_frame, height=24,
                                         bg=COLORS["bg_controls"], highlightthickness=0, bd=0)
        self.progress_canvas.pack(side="left", fill="x", expand=True, padx=(8, 4), pady=2)

        # Countdown (moved inside status frame, right side)
        countdown_col = tk.Frame(self.status_frame, bg=COLORS["bg_controls"])
        countdown_col.pack(side="right", padx=8, pady=2)
        self.countdown_label = tk.Label(countdown_col, text="",
                                        font=FONT["countdown"],
                                        bg=COLORS["bg_controls"], fg=COLORS["countdown_fg"])
        self.countdown_label.pack(anchor="e")
        self.countdown_bar = tk.Canvas(countdown_col, width=140, height=6,
                                       bg="#202030", highlightthickness=0, bd=0)
        self.countdown_bar.pack(anchor="e", pady=(2, 0))
        self._countdown_total = 0

        # Loading message below status
        self.loading_label = tk.Label(self.main_col, text="Loading radar images...",
                                      font=FONT["loading"], bg=COLORS["bg_controls"],
                                      fg=COLORS["loading_fg"])
        self.loading_label.pack(side="top", fill="x", pady=(0, 6))

        # Another separator
        tk.Frame(self.main_col, height=2, bg=COLORS["separator"]).pack(fill="x", pady=(0, 4))

        # --- MAIN PANELS AREA CONTAINER (empty until user picks locations) ---
        self.frame_container = tk.Frame(self.main_col, bg="", highlightthickness=0)
        self.frame_container.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        # Placeholder message shown until popup finishes
        self.intro_label = tk.Label(self.frame_container,
                                    text="Opening location / API setup...",
                                    fg="cyan", bg=COLORS["bg_root"],
                                    font=("Segoe UI", 14, "bold"))
        self.intro_label.pack(expand=True)

        # Defer location prompt so the root window is realized (fixes missing popup in EXE)
        self.locations = []
        self.panels = []
        self.radar_times = []
        self.loading_status = {}

        self.root.after(100, self.initialize_locations)  # schedule popup after a short delay

        self.frame_index = 0

        self.root.bind("<Escape>", lambda e: self.root.destroy())
        self.root.bind("<Configure>", self.on_resize)

        # Handle window close properly
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Do NOT start countdown / loading yet (no panels). They start after build_panels().


    # ---- Visuals ----
    def draw_gradient(self, w=None, h=None, force=False):
        if w is None or h is None:
            w = max(self.root.winfo_width(), 900)
            h = max(self.root.winfo_height(), 400)
        if not force and (w, h) == self._gradient_size and self._gradient_photo:
            # Already valid; just redraw image
            self.bg_canvas.delete("all")
            self.bg_canvas.create_image(0, 0, image=self._gradient_photo, anchor="nw")
            return
        self._gradient_size = (w, h)
        # Build vertical gradient efficiently: 1px wide then stretch
        from PIL import Image as _Img, ImageTk as _ImageTk
        grad_col = _Img.new("RGB", (1, h))
        top_rgb = (16, 16, 32)
        bottom_rgb = (16, 16, 96)
        p = grad_col.load()
        for i in range(h):
            t = i / (h - 1 if h > 1 else 1)
            r = int(top_rgb[0] + (bottom_rgb[0] - top_rgb[0]) * t)
            g = int(top_rgb[1] + (bottom_rgb[1] - top_rgb[1]) * t)
            b = int(top_rgb[2] + (bottom_rgb[2] - top_rgb[2]) * t)
            p[0, i] = (r, g, b)
        grad_full = grad_col.resize((w, h))
        self._gradient_photo = _ImageTk.PhotoImage(grad_full)
        self.bg_canvas.delete("all")
        self.bg_canvas.create_image(0, 0, image=self._gradient_photo, anchor="nw")

    def on_resize(self, event):
        if self.closing:
            return
        # Enter fast resize mode
        global FAST_RESIZE_MODE
        FAST_RESIZE_MODE = True

        # Cancel pending finalize if any
        if self._resize_idle_after_id:
            try:
                self.root.after_cancel(self._resize_idle_after_id)
            except Exception:
                pass
            self._resize_idle_after_id = None

        # Light-weight: do NOT redraw gradient each event (keeps old one visible)
        # Just attempt quick nearest scaling refresh for current frame
        for panel in self.panels:
            panel.show_frame(self.frame_index)

        # Schedule finalize after user stops resizing
        self._resize_idle_after_id = self.root.after(FAST_RESIZE_DEBOUNCE_MS, self._finalize_resize)

    def _finalize_resize(self):
        if self.closing:
            return
        global FAST_RESIZE_MODE
        FAST_RESIZE_MODE = False
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        self.draw_gradient(w, h, force=True)
        for panel in self.panels:
            panel.show_frame(self.frame_index)
        self._resize_idle_after_id = None

    # ---- Radar Animation ----
    def check_loading_complete(self):
        if self.closing:
            return
        if all(c == len(self.radar_times) for c in self.loading_status.values()) and self.radar_times:
            print("Radar images loaded!")
            if self.loading_label and self.loading_label.winfo_exists():
                try:
                    self.loading_label.destroy()
                except Exception:
                    pass
            # Kill any pending animation id so we restart clean
            self._cancel_after('_anim_after_id')
            self.frame_index = min(self.frame_index, max(0, len(self.radar_times) - 1))
            if not self.is_paused:
                self.animate()
        else:
            # store id so it can be cancelled on close
            self._check_loading_after_id = self._safe_after(150, self.check_loading_complete)

    def animate(self):
        if self.closing:
            return
        if self.is_paused:
            return
        # Ensure we have frames loaded
        if not (self.panels and self.radar_times and all(p.composite_images_pil for p in self.panels)):
            # Try again shortly
            self._anim_after_id = self._safe_after(250, self.animate)
            return

        if self.frame_index >= len(self.radar_times):
            self.frame_index = 0

        for panel in self.panels:
            panel.show_frame(self.frame_index)

        # Timestamp
        ts = self.radar_times[self.frame_index] if self.frame_index < len(self.radar_times) else None
        if ts:
            try:
                dt = datetime.fromtimestamp(int(ts), UTC)
                tstr = dt.strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                tstr = str(ts)
        else:
            tstr = "--"
        self.timestamp_label.config(
            text=f"Frame {self.frame_index+1}/{len(self.radar_times)} | Radar Time: {tstr}"
        )

        self.update_progress_bar()

        at_latest = (ts == self.radar_times[-1]) if ts else False
        if at_latest:
            # Pause briefly on newest frame, then loop from start
            delay = 3000
            self.frame_index = 0
        else:
            delay = ANIMATION_DELAY
            self.frame_index = (self.frame_index + 1) % max(1, len(self.radar_times))

        self._anim_after_id = self._safe_after(delay, self.animate, '_anim_after_id')

    def update_progress_bar(self):
        self.progress_canvas.delete("all")
        width = self.progress_canvas.winfo_width() or 300
        height = self.progress_canvas.winfo_height() or 24
        margin = 4
        bar_w = width - 2 * margin
        bar_h = height - 2 * margin
        if not self.radar_times:
            return
        progress = (self.frame_index + 1) / len(self.radar_times)
        fill_w = int(bar_w * progress)
        self.progress_canvas.create_rectangle(margin, margin, margin+bar_w, margin+bar_h,
                                              fill=COLORS["accent_progress_bg"],
                                              outline=COLORS["accent_progress_border"])
        self.progress_canvas.create_rectangle(margin, margin, margin+fill_w, margin+bar_h,
                                              fill=COLORS["accent_progress_fill"], outline="")
        knob_x = margin + fill_w
        self.progress_canvas.create_oval(knob_x-8, margin, knob_x+8, margin+bar_h,
                                         fill=COLORS["progress_knob_fill"], outline="")

    # ---- User Controls ----
    def toggle_pause(self):
        """Pause/resume animation; keeps current frame visible."""
        if self.closing:
            return
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.pause_btn.config(text="▶ Play")
            if getattr(self, "_anim_after_id", None):
                try:
                    self.root.after_cancel(self._anim_after_id)
                except Exception:
                    pass
                self._anim_after_id = None
        else:
            self.pause_btn.config(text="⏸ Pause")
            self.animate()
        # Update timestamp display
        if self.radar_times and self.frame_index < len(self.radar_times):
            ts = self.radar_times[self.frame_index]
            try:
                dt = datetime.fromtimestamp(int(ts), UTC)
                tstr = dt.strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                tstr = str(ts)
        else:
            tstr = "--"
        self.timestamp_label.config(
            text=f"Frame {self.frame_index+1}/{len(self.radar_times)} | Radar Time: {tstr}"
        )

    def step_prev(self):
        """Show previous radar frame (wrap)."""
        if self.closing or not self.radar_times:
            return
        # Pause animation if running
        if not self.is_paused:
            self.is_paused = True
            self.pause_btn.config(text="▶ Play")
            if getattr(self, "_anim_after_id", None):
                try: self.root.after_cancel(self._anim_after_id)
                except Exception: pass
                self._anim_after_id = None
        self.frame_index = (self.frame_index - 1) % len(self.radar_times)
        for p in self.panels:
            p.show_frame(self.frame_index)
        self.update_progress_bar()
        self._update_timestamp_label()

    def step_next(self):
        """Show next radar frame (wrap)."""
        if self.closing or not self.radar_times:
            return
        if not self.is_paused:
            self.is_paused = True
            self.pause_btn.config(text="▶ Play")
            if getattr(self, "_anim_after_id", None):
                try: self.root.after_cancel(self._anim_after_id)
                except Exception: pass
                self._anim_after_id = None
        self.frame_index = (self.frame_index + 1) % len(self.radar_times)
        for p in self.panels:
            p.show_frame(self.frame_index)
        self.update_progress_bar()
        self._update_timestamp_label()

    def _update_timestamp_label(self):
        """Refresh timestamp label for current frame."""
        if not self.radar_times or self.frame_index >= len(self.radar_times):
            self.timestamp_label.config(text="Frame --/-- | Radar Time: --")
            return
        ts = self.radar_times[self.frame_index]
        try:
            dt = datetime.fromtimestamp(int(ts), UTC)
            tstr = dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            tstr = str(ts)
        self.timestamp_label.config(
            text=f"Frame {self.frame_index+1}/{len(self.radar_times)} | Radar Time: {tstr}"
        )

    # ---- Loading & Scheduling ----
    def update_loading(self, location_name, loaded, total):
        def do_update():
            if not self.loading_label or not self.loading_label.winfo_exists():
                return
            self.loading_status[location_name] = loaded
            status = " | ".join(f"{n}: {self.loading_status.get(n,0)}/{len(self.radar_times)}"
                                for n in self.loading_status.keys())
            try:
                self.loading_label.config(text=f"Loading radar images... {status}")
            except Exception:
                pass
        self.root.after(0, do_update)

    def start_loading_label_slide(self):
        if self.closing or not self.loading_label.winfo_exists():
            return
        try:
            self.loading_label.update_idletasks()
            y = self.loading_label.winfo_y()
            h = self.loading_label.winfo_height()
            self.loading_label.pack_forget()
            self.loading_label.place(x=0, y=y, relwidth=1)
            self._loading_label_slide_y = y
            self._loading_label_height = h
            self.animate_loading_label_slide()
        except Exception:
            try: self.loading_label.destroy()
            except Exception: pass

    def animate_loading_label_slide(self):
        if self.closing or not self.loading_label.winfo_exists():
            return
        self._loading_label_slide_y -= 4
        self.loading_label.place_configure(y=self._loading_label_slide_y)
        if self._loading_label_slide_y + self._loading_label_height <= 0:
            try: self.loading_label.destroy()
            except Exception: pass
            return
        if not self.closing:
            self._loading_slide_after_id = self.root.after(16, self.animate_loading_label_slide)

    def show_loading_message(self, text, slide_after=None):
        # Simplified: only update label text (no slide). If label gone, just print.
        if self.closing:
            return
        if getattr(self, "loading_label", None) and self.loading_label.winfo_exists():
            try:
                self.loading_label.config(text=text)
            except Exception:
                print(text)
        else:
            print(text)

    def schedule_next_refresh(self, initial=False, fallback=False):
        if self.closing:
            return
        if getattr(self, "_refresh_after_id", None):
            try: self.root.after_cancel(self._refresh_after_id)
            except Exception: pass
            self._refresh_after_id = None
        now = time.time()
        delay_ms = REFRESH_INTERVAL
        if not fallback and self.radar_times and len(self.radar_times) >= 2:
            try:
                last = int(self.radar_times[-1]); prev = int(self.radar_times[-2])
                interval = max(60, min(900, last - prev))
                expected_next = last + interval
                target_time = expected_next + (DELAY_AFTER_FRAME_AVAILABLE_MS / 1000.0)
                delay_sec = max(5, target_time - now)
                delay_ms = int(delay_sec * 1000)
                print(f"[Scheduler] Last frames {prev}->{last} (Δ={interval}s). "
                      f"Next expected ~{interval}s later. Scheduling refresh in {delay_sec:.1f}s "
                      f"(includes 30s delay).")
            except Exception as e:
                print(f"[Scheduler] Predictive scheduling failed: {e}; using fallback {REFRESH_INTERVAL/1000:.0f}s.")
        else:
            if fallback:
                print(f"[Scheduler] Fallback scheduling in {REFRESH_INTERVAL/1000:.0f}s due to fetch error.")
        self.next_refresh_timestamp = now + (delay_ms / 1000.0)
        self._countdown_total = int(delay_ms / 1000.0)
        if not self.closing:
            self._refresh_after_id = self._safe_after(delay_ms, self.refresh_radar_frames)
        if initial:
            print(f"[Scheduler] Initial radar refresh scheduled in {delay_ms/1000:.1f}s.")

    def refresh_radar_frames(self):
        try:
            r = requests.get(RAINVIEWER_API, timeout=10)
            r.raise_for_status()
            new_times = [str(t["time"]) for t in r.json()["radar"]["past"]]
        except Exception as e:
            print(f"Error refreshing RainViewer radar times: {e}")
            self.schedule_next_refresh(fallback=True)
            return

        if new_times:
            existing = set(self.radar_times)
            appended = False
            for ts in new_times:
                if ts not in existing:
                    self.radar_times.append(ts)
                    appended = True
            if appended:
                for ts in new_times:
                    if ts not in existing:
                        try:
                            dt = datetime.fromtimestamp(int(ts), UTC)
                            print(f"[Radar] New frame acquired: {dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
                        except Exception:
                            print(f"[Radar] New frame acquired: {ts}")
                if len(self.radar_times) > EXTENDED_MAX_FRAMES:
                    overflow = len(self.radar_times) - EXTENDED_MAX_FRAMES
                    self.radar_times = self.radar_times[overflow:]
                self.loading_status = {loc["name"]: 0 for loc in self.locations}
                for panel in self.panels:
                    panel.radar_times = self.radar_times
                    threading.Thread(target=panel.load_radar_frames, daemon=True).start()
                self.frame_index = 0
                print("Updating radar images...")
                self.check_loading_complete()
        self.schedule_next_refresh()

    # ---- Extended Frames at Startup ----
    def _extend_with_cached_frames(self, times):
        if not times:
            return times
        try:
            earliest = int(times[0])
        except Exception:
            return times
        cached_ts = set()
        for loc in (self.locations if hasattr(self, "locations") else DEFAULT_LOCATIONS):
            safe_loc = ''.join(c if c.isalnum() else '_' for c in loc['name']).lower()
            cache_dir = os.path.join(BASE_DIR, "radar_cache", safe_loc)
            if not os.path.isdir(cache_dir):
                continue
            try:
                for f in os.listdir(cache_dir):
                    if f.endswith(".png"):
                        name = f.rsplit(".", 1)[0]
                        if name.isdigit():
                            val = int(name)
                            if val < earliest:
                                cached_ts.add(val)
            except Exception:
                pass
        if not cached_ts:
            return times
        merged = sorted(cached_ts) + [int(t) for t in times]
        if len(merged) > EXTENDED_MAX_FRAMES:
            merged = merged[-EXTENDED_MAX_FRAMES:]
        out = [str(t) for t in merged]
        if out != times:
            print(f"[Startup] Extended frames using cache: {len(times)} -> {len(out)}")
        return out

    # ---- Countdown ----
    def update_countdown(self):
        if self.closing:
            return
        now = time.time()
        remaining = int(self.next_refresh_timestamp - now)
        if remaining >= 0:
            mins, secs = divmod(remaining, 60)
            try:
                self.countdown_label.config(text=f"Next radar: {mins:02d}:{secs:02d}")
                if self._countdown_total > 0 and self.countdown_bar.winfo_exists():
                    frac = max(0.0, min(1.0, (self._countdown_total - remaining) / self._countdown_total))
                    w = self.countdown_bar.winfo_width() or 140
                    self.countdown_bar.delete("all")
                    self.countdown_bar.create_rectangle(0, 0, w, 6, fill="#303848", outline="")
                    self.countdown_bar.create_rectangle(0, 0, int(w * frac), 6, fill="#00d0ff", outline="")
            except Exception:
                pass



        else:
            try:
                self.countdown_label.config(text="Checking for new radar frame...")
                if self.countdown_bar.winfo_exists():
                    self.countdown_bar.delete("all")
            except Exception:
                pass
        self._countdown_after_id = self._safe_after(1000, self.update_countdown, '_countdown_after_id')

    def initialize_locations(self):
        """Prompt user (deferred so main window is realized) and then build panels."""
        if self.closing:
            return
        try:
            self.locations = prompt_for_locations(self.root, max_cities=2)
        except Exception as e:
            print(f"[Init] Location prompt failed: {e}; using defaults.")
            self.locations = DEFAULT_LOCATIONS[:2]
        self.build_panels()

    def build_panels(self):
        """Create radar panels after locations known and start loading logic."""
        if self.closing:
            return

        # Remove placeholder intro label if present
        if getattr(self, "intro_label", None) and self.intro_label.winfo_exists():
            try:
                self.intro_label.destroy()
            except Exception:
                pass

        # Clear any old children in container
        for child in self.frame_container.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass

        # Get initial radar times (extend with cache)
        base_times = get_latest_radar_times()
        self.radar_times = self._extend_with_cached_frames(base_times)
        print(f"[Startup] Extended frames using cache: {len(base_times)} -> {len(self.radar_times)}")

        self.loading_status = {loc["name"]: 0 for loc in self.locations}
        self.panels = []

        created_panels = []
        for loc in self.locations:
            p = RadarPanel(self.frame_container, loc, self.radar_times,
                           self.update_loading, self.get_panel_size)
                      

            created_panels.append(p)

        # Convert from the initial pack in RadarPanel to grid layout
        for child in self.frame_container.winfo_children():
            try:
                child.pack_forget()

            except Exception:
                pass

        if len(created_panels) == 1:
            # Single panel full width
            self.frame_container.grid_columnconfigure(0, weight=1, uniform="panels")
            self.frame_container.grid_rowconfigure(0, weight=1)
            created_panels[0].panel_frame.grid(row=0, column=0, sticky="nsew")
            self.center_bar_frame = None
        else:
            # Two panels + center divider
            self.frame_container.grid_columnconfigure(0, weight=1, uniform="panels")
            self.frame_container.grid_columnconfigure(1, weight=0, minsize=8)
            self.frame_container.grid_columnconfigure(2, weight=1, uniform="panels")
            self.frame_container.grid_rowconfigure(0, weight=1)
            created_panels[0].panel_frame.grid(row=0, column=0, sticky="nsew")
            self.center_bar_frame = tk.Frame(self.frame_container,
                                             bg=COLORS["center_bar"], width=8,
                                             highlightthickness=0, bd=0)
            self.center_bar_frame.grid(row=0, column=1, sticky="ns")
            created_panels[1].panel_frame.grid(row=0, column=2, sticky="nsew")
            self.center_bar_frame.lift()

        for p in created_panels:
            p.panel_frame.configure(padx=4)

        self.panels = created_panels
        self.frame_index = 0

        # Launch radar frame loading ONCE per panel (was duplicated before)
        for panel in self.panels:
            threading.Thread(target=panel.load_radar_frames, daemon=True).start()

        # Start scheduling / countdown / animation readiness checks
        self._refresh_after_id = None
        self.next_refresh_timestamp = time.time() + (REFRESH_INTERVAL / 1000)
        self.schedule_next_refresh(initial=True)
        self.update_countdown()
        self.check_loading_complete()

        # Initial frame display (may be blank until images arrive)
        for p in self.panels:
            p.show_frame(self.frame_index)

        print(f"[Init] Panels built for {len(self.locations)} location(s).")

    # ---- Size helper ----
    def get_panel_size(self):
        total_w = self.root.winfo_width()
        total_h = self.root.winfo_height() - 160
        count = max(1, len(getattr(self, "locations", [])))
        panel_w = max(128, int(total_w / count))
        panel_h = max(128, int(total_h))
        return panel_w, panel_h

    # ---- Shutdown ----
    def on_close(self):
        if self.closing:
            return
        self.closing = True
        # Cancel all scheduled callbacks
        for attr in ('_anim_after_id', '_refresh_after_id', '_countdown_after_id',
                     '_check_loading_after_id', '_resize_idle_after_id'):
            self._cancel_after(attr)
        try:
            for panel in self.panels:
                if hasattr(panel, "graph_figure") and panel.graph_figure:
                    plt.close(panel.graph_figure)
        except Exception:
            pass
        try:
            requests_session.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # --- PATCH: Add missing helper methods, after-id attributes, and main entry point ---

    def _safe_after(self, ms, func, attr_name=None):
        """Schedule a Tk 'after' callback only if app not closing. Stores id if attr_name given."""
        if self.closing:
            return None
        try:
            aid = self.root.after(ms, func)
            if attr_name:
                setattr(self, attr_name, aid)
            return aid
        except Exception:
            return None

    def _cancel_after(self, attr_name):
        """Cancel a previously stored after id."""
        aid = getattr(self, attr_name, None)
        if aid:
            try:
                self.root.after_cancel(aid)
            except Exception:
                pass
            setattr(self, attr_name, None)

# 2) In RadarApp.__init__ AFTER: self.root.protocol("WM_DELETE_WINDOW", self.on_close)
#    add initialization for the after-id attributes (if not already present):

        self._anim_after_id = None
        self._refresh_after_id = None
        self._countdown_after_id = None
        self._check_loading_after_id = None
        self._resize_idle_after_id = None

        print("[Startup] Application initialized; awaiting location prompt.")

# 3) Ensure all scheduling calls use _safe_after (you already did in animate / countdown / check_loading_complete / schedule_next_refresh / on_resize finalize).
#    If any direct self.root.after(...) remain for repeating tasks, convert them similarly.

# 4) ADD the main entry block at very end of the file (if missing):

if __name__ == "__main__":
    try:
        root = tk.Tk()
    except Exception as e:
        print(f"[Startup] Failed to create Tk root: {e}")
        sys.exit(1)
    app = RadarApp(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.on_close()
        print("[Shutdown] Interrupted by user (Ctrl+C).")
    except Exception as e:
        print(f"[Shutdown] Unexpected error: {e}")
        app.on_close()