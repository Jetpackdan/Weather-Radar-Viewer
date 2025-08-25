import requests
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from PIL import Image, ImageTk, ImageDraw
import io
import math
import os, sys
# Stable base directory: use exe folder if frozen, else script folder
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
import threading
import time
from datetime import datetime, UTC
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None

RAINVIEWER_API = "https://api.rainviewer.com/public/weather-maps.json"
TILE_URL_TEMPLATE = "https://tilecache.rainviewer.com/v2/radar/{time}/256/{z}/{lat}/{lon}/2/1_1.png"
GOOGLE_MAPS_TEMPLATE = (
    "https://maps.googleapis.com/maps/api/staticmap?"
    "center={lat},{lon}&zoom={z}&size=256x256&maptype=roadmap&key={api_key}"
)
GOOGLE_MAPS_API_KEY = "AIzaSyD3oN5YeEhwEQvmKkN0fNY-EHm6uBa11Qk"

LOCATIONS = [
    {"name": "Portland, OR", "lat": 45.5152, "lon": -122.6784},
    {"name": "Klamath Falls, OR", "lat": 42.2249, "lon": -121.7817}
]

# Points of interest (red pins) provided as DMS converted to decimal:
# 45°33'50.9"N 122°41'29.3"W  -> 45.5641389, -122.6914722
# 42°15'19.4"N 121°47'03.5"W  -> 42.2553889, -121.7843052
INTEREST_PINS = [
    (45.5641389, -122.6914722),
    (42.2553889, -121.7843052),
]

REFRESH_INTERVAL = 30000   # Fallback poll interval (ms)
DELAY_AFTER_FRAME_AVAILABLE_MS = 30000  # Wait 30s after expected new frame time before fetching
ANIMATION_DELAY = 120      # ms between frames
EXTENDED_MAX_FRAMES = 50  # Number of frames to retain locally for extended animation
FORECAST_REFRESH_MS = 30 * 60 * 1000  # 30 minutes

# Forecast cache
_forecast_cache = {}
_forecast_cache_time = {}
def get_hourly_forecast(lat, lon):
    """Return structured 12-hour local forecast using NOAA NWS API.
    Structure: {
       'timezone': <IANA tz name>,
       'entries': [ { 'time': datetime (tz-aware), 'temp': float, 'weather': str, 'wind_speed': float, 'wind_dir': int, 'pop': float } ...]
    }
    On error returns {'timezone': 'UTC', 'entries': []}.
    """
    key = (lat, lon)
    if key in _forecast_cache:
        # Serve cached if fresher than refresh interval
        ts = _forecast_cache_time.get(key, 0)
        if (time.time() - ts) < (FORECAST_REFRESH_MS / 1000.0):
            return _forecast_cache[key]
    try:
        # Step 1: Get gridpoint for lat/lon
        url_points = f"https://api.weather.gov/points/{lat},{lon}"
        r_points = requests_session.get(url_points, timeout=10, headers={"User-Agent": "RadarViewer/1.0"})
        r_points.raise_for_status()
        data_points = r_points.json()
        forecast_hourly_url = data_points["properties"]["forecastHourly"]
        tz_name = data_points["properties"].get("timeZone", "UTC")
        # Step 2: Get hourly forecast
        r_forecast = requests_session.get(forecast_hourly_url, timeout=10, headers={"User-Agent": "RadarViewer/1.0"})
        r_forecast.raise_for_status()
        data_forecast = r_forecast.json()
        periods = data_forecast["properties"]["periods"][:12]
        from datetime import datetime
        entries = []
        for p in periods:
            # p: dict with startTime, temperature, windSpeed, windDirection, shortForecast, probabilityOfPrecipitation
            try:
                dt = datetime.fromisoformat(p["startTime"].replace("Z", "+00:00"))  # UTC
                if ZoneInfo and tz_name:
                    try:
                        dt = dt.astimezone(ZoneInfo(tz_name))
                    except Exception:
                        pass
            except Exception:
                dt = p["startTime"]
            temp = p.get("temperature")
            temp_unit = p.get("temperatureUnit", "F")
            # Convert to Celsius if needed
            if temp is not None and temp_unit == "F":
                temp = (temp - 32) * 5.0 / 9.0
            weather = p.get("shortForecast", "Unknown")
            pop = p.get("probabilityOfPrecipitation", {}).get("value")
            wind_speed = None
            wind_dir = None
            # Parse wind speed (e.g., "7 mph")
            ws_txt = p.get("windSpeed", "")
            if ws_txt:
                try:
                    ws_val = float(ws_txt.split()[0])
                    wind_speed = ws_val * 0.44704  # mph to m/s
                except Exception:
                    wind_speed = None
            # Parse wind direction (e.g., "NW")
            wd_txt = p.get("windDirection", "")
            # Map compass to degrees
            compass_map = {
                "N": 0, "NNE": 22, "NE": 45, "ENE": 67, "E": 90, "ESE": 112, "SE": 135, "SSE": 157,
                "S": 180, "SSW": 202, "SW": 225, "WSW": 247, "W": 270, "WNW": 292, "NW": 315, "NNW": 337
            }
            wind_dir = compass_map.get(wd_txt, None)
            entries.append({
                'time': dt,
                'temp': temp,
                'weather': weather,
                'pop': pop,
                'wind_speed': wind_speed,
                'wind_dir': wind_dir
            })
        result = {"timezone": tz_name, "entries": entries}
        _forecast_cache[key] = result
        _forecast_cache_time[key] = time.time()
        return result
    except Exception as e:
        print(f"Error fetching NOAA forecast for {lat},{lon}: {e}")
        return {"timezone": "UTC", "entries": []}

class RadarPanel:
    def __init__(self, parent, location, radar_times, loading_callback, get_size_callback):
        self.location = location
        self.radar_times = radar_times
        self.loading_callback = loading_callback
        self.get_size_callback = get_size_callback

        # Container
        self.panel_frame = tk.Frame(parent, bg="black")
        self.panel_frame.pack(side="left", fill="both", expand=True)

        # Header label
        self.label = tk.Label(self.panel_frame, bg="black", text=location["name"], fg="white", font=("Arial", 14))
        self.label.pack(fill="x", expand=False)

        # Manual forecast refresh button
        self.refresh_btn = tk.Button(
            self.panel_frame,
            text="↻ Forecast",
            command=self.manual_refresh_forecast,
            font=("Segoe UI", 9),
            bg="#203050",
            fg="white",
            relief="flat"
        )
        self.refresh_btn.pack(fill="x", padx=2, pady=(0, 4))

        # Alerts controls
        self.show_alerts_var = tk.BooleanVar(value=False)
        self.alerts_checkbox = None
        self.alerts_frame = tk.Frame(self.panel_frame, bg="black")
        self.alerts_frame.pack(fill="x", expand=False)

        # Image area
        self.composite_image_label = tk.Label(self.panel_frame, bg="black")
        self.composite_image_label.pack(side="right", fill="both", expand=True)
        self.composite_images_pil = []
        self.composite_images_tk = []
        self.frame_index = 0
        self.map_image_pil = None

        # Forecast/info side panel
        self.info_frame = tk.Frame(self.panel_frame, bg="#101020")
        self.info_frame.pack(side="left", fill="y", padx=(6, 0), pady=(0, 0))
        self._forecast_after_id = None

        # Start loading resources
        self.load_map()
        threading.Thread(target=self.load_radar_frames, daemon=True).start()
        self.panel_frame.after(600, self.update_forecast)
        self.panel_frame.after(800, self.check_alerts_and_update_ui)

    def check_alerts_and_update_ui(self):
        alerts = self.get_noaa_alerts()
        # Remove checkbox if present
        if self.alerts_checkbox and self.alerts_checkbox.winfo_exists():
            self.alerts_checkbox.pack_forget()
        # Only show checkbox if there are alerts
        if alerts:
            if not self.alerts_checkbox:
                self.alerts_checkbox = tk.Checkbutton(
                    self.panel_frame,
                    text="Show Alerts",
                    variable=self.show_alerts_var,
                    command=self.toggle_alerts,
                    bg="black",
                    fg="red",
                    selectcolor="#222",
                    font=("Arial", 10)
                )
            self.alerts_checkbox.pack(fill="x", anchor="w", padx=2)
        # If checkbox is checked, show alerts
        if self.show_alerts_var.get() and alerts:
            self.update_alerts(alerts)
        else:
            for w in self.alerts_frame.winfo_children():
                w.destroy()
            self.alerts_frame.pack_forget()

    def toggle_alerts(self):
        alerts = self.get_noaa_alerts()
        if self.show_alerts_var.get() and alerts:
            self.update_alerts(alerts)
            self.alerts_frame.pack(fill="x", expand=False)
        else:
            for w in self.alerts_frame.winfo_children():
                w.destroy()
            self.alerts_frame.pack_forget()

    def update_alerts(self, alerts):
        for w in self.alerts_frame.winfo_children():
            w.destroy()
        if alerts:
            for alert in alerts:
                tk.Label(self.alerts_frame, text=alert, fg="red", bg="black", font=("Arial", 10, "bold"), wraplength=220, justify="left").pack(side="top", anchor="w", padx=2, pady=1)

    def get_noaa_alerts(self):
        # Use NOAA API for alerts for the location
        lat = self.location["lat"]
        lon = self.location["lon"]
        url = f"https://api.weather.gov/alerts/active?point={lat},{lon}"
        try:
            r = requests_session.get(url, timeout=10, headers={"User-Agent": "RadarViewer/1.0"})
            r.raise_for_status()
            data = r.json()
            features = data.get("features", [])
            alerts = []
            for f in features:
                props = f.get("properties", {})
                headline = props.get("headline")
                event = props.get("event")
                desc = props.get("description")
                # Prefer headline, else event, else short description
                if headline:
                    alerts.append(headline)
                elif event:
                    alerts.append(event)
                elif desc:
                    alerts.append(desc[:60] + "..." if len(desc) > 60 else desc)
            return alerts
        except Exception:
            return []

    # ----------------- Map & Radar Loading -----------------
    def load_map(self):
        z = 8
        map_url = GOOGLE_MAPS_TEMPLATE.format(
            lat=self.location["lat"],
            lon=self.location["lon"],
            z=z,
            api_key=GOOGLE_MAPS_API_KEY
        )
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            r_map = requests_session.get(map_url, timeout=10, headers=headers)
            r_map.raise_for_status()
            self.map_image_pil = Image.open(io.BytesIO(r_map.content)).convert("RGBA")
            try:
                self._add_interest_pins(self.map_image_pil, z)
            except Exception as e:
                print(f"Pin overlay error for {self.location['name']}: {e}")
        except Exception as e:
            print(f"Error loading Google map for {self.location['name']}: {e}")
            self.map_image_pil = None

    def _add_interest_pins(self, img, zoom):
        """Overlay red pins for INTEREST_PINS on the static map image.
        Uses Web Mercator projection to translate lat/lon to pixel offsets.
        """
        # Make idempotent: skip if we've already added pins to this PIL Image instance
        if not img or getattr(img, '_pins_added', False):
            return
        width, height = img.size  # expected 256x256
        if width == 0 or height == 0:
            return
        # Helper: lat/lon -> world pixels at given zoom
        def world_px(lat, lon, z):
            scale = 256 * (2 ** z)
            x = (lon + 180.0) / 360.0 * scale
            siny = math.sin(math.radians(lat))
            # Clamp siny to valid range just in case
            siny = min(max(siny, -0.9999), 0.9999)
            y = (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * scale
            return x, y
        center_lat = self.location['lat']
        center_lon = self.location['lon']
        cx, cy = world_px(center_lat, center_lon, zoom)
        draw = ImageDraw.Draw(img)
        for plat, plon in INTEREST_PINS:
            px, py = world_px(plat, plon, zoom)
            dx = px - cx
            dy = py - cy
            # Translate to image pixels (center of map at width/2, height/2)
            ix = int(round(width / 2 + dx))
            iy = int(round(height / 2 + dy))
            if -5 <= ix <= width + 5 and -5 <= iy <= height + 5:
                # Simple dot: white outer ring + red center (no stem)
                r_outer = 5
                r_inner = 3
                draw.ellipse((ix - r_outer, iy - r_outer, ix + r_outer, iy + r_outer), fill="white")
                draw.ellipse((ix - r_inner, iy - r_inner, ix + r_inner, iy + r_inner), fill="#ff2222")
        # Mark so we don't double draw on same instance
        setattr(img, '_pins_added', True)

    def load_radar_frames(self):
        """Load radar frames (with caching) and build composite images list."""
        self.composite_images_pil.clear()
        self.composite_images_tk.clear()
        if not self.map_image_pil:
            return
        z = 8
        headers = {"User-Agent": "Mozilla/5.0"}
        safe_loc = ''.join(c if c.isalnum() else '_' for c in self.location['name']).lower()
        cache_dir = os.path.join(BASE_DIR, 'radar_cache', safe_loc)
        os.makedirs(cache_dir, exist_ok=True)
        print(f"[Cache] Using cache directory for {self.location['name']}: {cache_dir}")

        def cache_path(ts):
            return os.path.join(cache_dir, f"{ts}.png")

        total_count = len(self.radar_times)

        def load_or_fetch(ts):
            path = cache_path(ts)
            if os.path.exists(path):
                try:
                    cached = Image.open(path).convert("RGBA")
                    # Ensure pins are present (idempotent)
                    try:
                        self._add_interest_pins(cached, z)
                    except Exception:
                        pass
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
                # Start from a copy of base map with pins so pins stay on top
                base_with_pins = self.map_image_pil.copy()
                try:
                    # Reapply pins (map already had them, but ensure visibility on top of radar)
                    self._add_interest_pins(base_with_pins, z)
                except Exception:
                    pass
                composite = Image.alpha_composite(base_with_pins, radar_img)
                try:
                    composite.save(path, format='PNG')
                    print(f"[Cache] Saved radar frame for {self.location['name']} at {path}")
                except Exception as e:
                    print(f"Cache save failed {path}: {e}")
                return composite
            except Exception as e:
                if os.path.exists(path):
                    try:
                        return Image.open(path).convert("RGBA")
                    except Exception:
                        pass
                print(f"Radar fetch failed for {self.location['name']} {ts}: {e}")
                img = self.map_image_pil.copy()
                draw = ImageDraw.Draw(img)
                w, h = img.size
                draw.line((0, 0, w, h), fill=(255, 0, 0, 80), width=6)
                draw.line((0, h, w, 0), fill=(255, 0, 0, 80), width=6)
                return img

        for idx, ts in enumerate(self.radar_times):
            img = load_or_fetch(ts)
            # Safety: ensure pins for every frame (cached ones may have been saved before pin feature existed)
            try:
                self._add_interest_pins(img, z)
            except Exception:
                pass
            self.composite_images_pil.append(img)
            self.loading_callback(self.location['name'], idx + 1, total_count)

        try:
            cached_files = [f for f in os.listdir(cache_dir) if f.endswith('.png')]
            parsed = [int(f.rsplit('.', 1)[0]) for f in cached_files if f.rsplit('.', 1)[0].isdigit()]
            parsed.sort()
            if len(parsed) > EXTENDED_MAX_FRAMES:
                to_delete = parsed[0:len(parsed)-EXTENDED_MAX_FRAMES]
                for ts in to_delete:
                    fp = cache_path(ts)
                    try:
                        os.remove(fp)
                    except Exception:
                        pass
        except Exception as e:
            print(f"Cache trim error for {self.location['name']}: {e}")

        self.update_scaled_images()

    def update_scaled_images(self):
        # Only once (no dynamic resize)
        if not self.composite_images_tk:
            for img in self.composite_images_pil:
                self.composite_images_tk.append(ImageTk.PhotoImage(img))

    def show_frame(self, frame_index):
        total = len(self.composite_images_tk)
        if total == 0:
            return
        if frame_index >= total:
            frame_index = total - 1
        if self.composite_images_tk and getattr(self, '_last_frame', None) != frame_index:
            self.composite_image_label.config(image=self.composite_images_tk[frame_index])
            self._last_frame = frame_index

        # Show missing radar message if this frame is a map with X
        # Check if the image is a map with X by looking for the faint X (red lines)
        # We'll use a flag: if radar fetch failed, we set a flag in composite_images_pil
        # Instead, let's check if the image is identical to map_image_pil (approximate)
        # We'll set a message if the frame is missing radar
        if hasattr(self, 'missing_label') and self.missing_label.winfo_exists():
            self.missing_label.destroy()
        if frame_index >= len(self.composite_images_pil):
            return
        img = self.composite_images_pil[frame_index]
        # Heuristic: if the image is not from cache and has a faint X, show message
        # We'll check if the image is not from cache by checking if the pixel at (10,10) is reddish and not like the map
        try:
            px = img.getpixel((10,10))
            # If the red channel is high and alpha is not 0, likely the X
            if px[0] > 200 and px[1] < 100 and px[2] < 100 and px[3] > 50:
                self.missing_label = tk.Label(self.panel_frame, text="No radar image available for this time. The X means radar data is missing.", fg="#ff6666", bg="black", font=("Arial", 8), wraplength=220, justify="center")
                self.missing_label.pack(side="bottom", fill="x", pady=(2, 2))
        except Exception:
            pass

    # ----------------- Forecast Rendering -----------------
    def update_forecast(self):
        """Fetch forecast, render grid + embedded temp/precip graph."""
        data = get_hourly_forecast(self.location["lat"], self.location["lon"])
        tz_name = data.get("timezone", "") if isinstance(data, dict) else ""
        entries = data.get("entries", []) if isinstance(data, dict) else []

        # Clear previous children
        for w in self.info_frame.winfo_children():
            w.destroy()

        if not entries:
            tk.Label(self.info_frame, text="Forecast unavailable", fg="white", bg="#101020", font=("Segoe UI", 10)).pack(anchor="w")
            return

        # Timezone abbreviation
        try:
            first_dt = entries[0]["time"]
            if hasattr(first_dt, 'strftime'):
                tz_abbr = first_dt.strftime('%Z') or tz_name.split('/')[-1]
            else:
                tz_abbr = tz_name.split('/')[-1]
        except Exception:
            tz_abbr = tz_name.split('/')[-1] if tz_name else ""

        header = tk.Label(self.info_frame, text=f"Next 12 hrs ({tz_abbr})", fg="cyan", bg="#101020", font=("Segoe UI", 10, 'bold'))
        header.pack(anchor="w")

        # Grid
        columns = ["Hour", "Temp", "Weather", "Precip", "Wind"]
        grid_frame = tk.Frame(self.info_frame, bg="#101020")
        grid_frame.pack(fill="x")
        for col, name in enumerate(columns):
            tk.Label(grid_frame, text=name, fg="cyan", bg="#101020", font=("Segoe UI", 9, "bold"), borderwidth=1, relief="ridge", padx=2, pady=2).grid(row=0, column=col, sticky="nsew")

        for i, entry in enumerate(entries):
            dt_obj = entry["time"]
            temp = entry["temp"]
            weather = entry["weather"]
            pop = entry.get("pop")
            wind_speed = entry.get("wind_speed")
            wind_dir = entry.get("wind_dir")
            if hasattr(dt_obj, 'strftime'):
                hour_str = dt_obj.strftime('%a %I%p').lstrip('0')
            else:
                hour_str = str(dt_obj)
            precip_txt = f"{int(pop)}%" if isinstance(pop, (int, float)) else ""
            wind_txt = ""
            if wind_speed is not None and wind_dir is not None:
                arrows = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"]
                idx = int(((int(wind_dir) + 22.5) % 360) // 45)
                wind_txt = f"{arrows[idx]} {wind_speed:.1f}m/s"
            weather_short = weather if len(str(weather)) <= 16 else str(weather)[:13] + "..."
            row_data = [hour_str, f"{temp:.0f}°C", weather_short, precip_txt, wind_txt]
            for col, val in enumerate(row_data):
                tk.Label(grid_frame, text=val, fg="white", bg="#101020", font=("Segoe UI", 9), borderwidth=1, relief="groove", padx=2, pady=2).grid(row=i+1, column=col, sticky="nsew")

        # Legend
        try:
            start_dt = entries[0]["time"]; end_dt = entries[-1]["time"]
            if hasattr(start_dt, 'strftime') and hasattr(end_dt, 'strftime'):
                legend_text = f"Covers {start_dt.strftime('%a %I:%M %p').lstrip('0')} to {end_dt.strftime('%I:%M %p').lstrip('0')}"
            else:
                legend_text = f"Starting {start_dt}"
            tk.Label(self.info_frame, text=legend_text, fg="#888", bg="#101020", font=("Segoe UI", 8)).pack(anchor="w", pady=(4,0))
        except Exception:
            pass

        # Graph data
        temps = [e["temp"] for e in entries]
        pops = [ (e.get("pop") or 0) for e in entries]
        hours = []
        for e in entries:
            dt_obj = e["time"]
            if hasattr(dt_obj, 'strftime'):
                hours.append(dt_obj.strftime('%I%p').lstrip('0'))
            else:
                hours.append(str(dt_obj))
        xvals = list(range(len(hours)))

        # Cleanup previous graph
        if hasattr(self, 'graph_canvas'):
            try:
                if self.graph_canvas:
                    self.graph_canvas.get_tk_widget().destroy()
                if hasattr(self, 'graph_figure') and self.graph_figure:
                    plt.close(self.graph_figure)
            except Exception:
                pass

        fig, ax1 = plt.subplots(figsize=(3, 2.0), dpi=100)
        ax1.plot(xvals, temps, color='tab:red', marker='o')
        ax1.set_ylabel('Temp (°C)', color='tab:red')
        ax1.tick_params(axis='y', labelcolor='tab:red')
        ax1.set_xticks(xvals)
        ax1.set_xticklabels(hours, rotation=45, fontsize=8)
        ax1.set_ylim(bottom=0)
        ax1.set_xlim(-0.2, len(xvals)-0.8)

        ax2 = ax1.twinx()
        ax2.plot(xvals, pops, color='tab:blue', marker='x', linestyle='--')
        ax2.set_ylabel('Precip (%)', color='tab:blue')
        ax2.tick_params(axis='y', labelcolor='tab:blue')
        ax2.set_ylim(0, 100)

        fig.tight_layout()
        canvas = FigureCanvasTkAgg(fig, master=self.info_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(side="bottom", fill="x", pady=6)
        self.graph_canvas = canvas
        self.graph_figure = fig
        # Schedule next automatic refresh (avoid duplicates)
        try:
            if self._forecast_after_id:
                try:
                    self.info_frame.after_cancel(self._forecast_after_id)
                except Exception:
                    pass
            self._forecast_after_id = self.info_frame.after(FORECAST_REFRESH_MS, self.update_forecast)
        except Exception:
            pass

    def manual_refresh_forecast(self):
        # Clear cached forecast for this location then update immediately
        try:
            key = (self.location["lat"], self.location["lon"])
            _forecast_cache.pop(key, None)
            _forecast_cache_time.pop(key, None)
        except Exception:
            pass
        self.update_forecast()

requests_session = requests.Session()


class RadarApp:
    def __init__(self, root):
        # Root / window setup
        self.root = root
        self.root.title("Oregon Radar: Portland & Klamath Falls (Animated Layered Radar + Google Maps)")
        self.root.geometry("900x400")
        self.closing = False

        # Gradient background
        self.bg_canvas = tk.Canvas(self.root, width=900, height=400, highlightthickness=0, bd=0)
        self.bg_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.draw_gradient()

        # Timestamp label
        self.timestamp_label = tk.Label(self.root, text="", font=("Segoe UI", 16, "bold"), bg="#101020", fg="cyan")
        self.timestamp_label.pack(side="top", fill="x", pady=6)

        # Animation controls
        controls_frame = tk.Frame(self.root, bg="#101020")
        controls_frame.pack(side="top", fill="x", pady=4)
        self.is_paused = False
        self.pause_btn = tk.Button(controls_frame, text="⏸ Pause", font=("Segoe UI", 10), command=self.toggle_pause, bg="#1a2340", fg="white", relief="flat")
        self.pause_btn.pack(side="left", padx=6, pady=2)
        self.prev_btn = tk.Button(controls_frame, text="−", font=("Segoe UI", 12, "bold"), width=2, command=self.step_prev, bg="#1a2340", fg="white", relief="flat")
        self.prev_btn.pack(side="left", padx=2)
        self.next_btn = tk.Button(controls_frame, text="+", font=("Segoe UI", 12, "bold"), width=2, command=self.step_next, bg="#1a2340", fg="white", relief="flat")
        self.next_btn.pack(side="left", padx=2)

        # Animation frame progress bar
        self.progress_canvas = tk.Canvas(self.root, height=24, bg="#101020", highlightthickness=0)
        self.progress_canvas.pack(side="top", fill="x", pady=4)

        # Loading banner
        self.loading_label = tk.Label(self.root, text="Loading radar images...", font=("Segoe UI", 14), bg="#101020", fg="#ffcc00")
        self.loading_label.pack(side="top", fill="x", pady=8)

        # Main container for panels
        self.frame = tk.Frame(self.root, bg="", highlightthickness=0)
        self.frame.pack(fill="both", expand=True, padx=12, pady=8)

        # Countdown label & bar (top-right)
        self.countdown_label = tk.Label(self.root, text="", font=("Segoe UI", 12), bg="#101020", fg="cyan")
        self.countdown_label.place(relx=1.0, rely=0.0, anchor="ne", x=-10, y=10)
        self.countdown_bar = tk.Canvas(self.root, width=140, height=6, bg="#202030", highlightthickness=0, bd=0)
        self.countdown_bar.place(relx=1.0, rely=0.0, anchor="ne", x=-10, y=34)
        self._countdown_total = 0

        # Data initialization
        self.radar_times = self._extend_with_cached_frames(self.get_latest_radar_times())
        self.loading_status = {loc["name"]: 0 for loc in LOCATIONS}
        self.panel_width = 256
        self.panel_height = 256
        self.panels = [
            RadarPanel(self.frame, LOCATIONS[0], self.radar_times, self.update_loading, self.get_panel_size),
            RadarPanel(self.frame, LOCATIONS[1], self.radar_times, self.update_loading, self.get_panel_size)
        ]
        self.frame_index = 0

        # Key bindings
        self.root.bind("<Escape>", lambda e: self.root.destroy())
        self.root.bind("<Configure>", self.on_resize)

        # Load state check
        self.check_loading_complete()

        # Scheduling
        self._refresh_after_id = None
        self.next_refresh_timestamp = time.time() + (REFRESH_INTERVAL/1000)
        self.schedule_next_refresh(initial=True)
        self.update_countdown()

        # Window close protocol
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ----------------- Clean Shutdown -----------------
    def on_close(self):
        if self.closing:
            return
        self.closing = True
        # Cancel scheduled after callbacks if present
        for attr in (
            '_anim_after_id',
            '_refresh_after_id',
            '_pending_slide_after_id',
            '_countdown_after_id',
            '_loading_slide_after_id',
            '_start_slide_after_id'
        ):
            aid = getattr(self, attr, None)
            if aid:
                try:
                    self.root.after_cancel(aid)
                except Exception:
                    pass
                setattr(self, attr, None)
        # Stop countdown recursion by not scheduling further
        # Close any matplotlib figures
        try:
            for panel in getattr(self, 'panels', []):
                if hasattr(panel, 'graph_figure') and panel.graph_figure:
                    try:
                        plt.close(panel.graph_figure)
                    except Exception:
                        pass
        except Exception:
            pass
        # Close requests session
        try:
            requests_session.close()
        except Exception:
            pass
        # Destroy root
        try:
            self.root.destroy()
        except Exception:
            pass

    # Override recurring methods to respect closing flag
    def update_countdown(self):
        if getattr(self, 'closing', False):
            return
        now = time.time()
        remaining = int(self.next_refresh_timestamp - now)
        if remaining >= 0:
            try:
                mins, secs = divmod(remaining, 60)
                self.countdown_label.config(text=f"Next radar: {mins:02d}:{secs:02d}")
                # Update bar
                if self._countdown_total > 0 and self.countdown_bar.winfo_exists():
                    frac = max(0.0, min(1.0, (self._countdown_total - remaining) / self._countdown_total))
                    w = self.countdown_bar.winfo_width() or 140
                    self.countdown_bar.delete("all")
                    self.countdown_bar.create_rectangle(0, 0, w, 6, fill="#303848", outline="")
                    self.countdown_bar.create_rectangle(0, 0, int(w * frac), 6, fill="#00d0ff", outline="")
            except Exception:
                return
        else:
            try:
                self.countdown_label.config(text="Checking for new radar frame...")
                if self.countdown_bar.winfo_exists():
                    self.countdown_bar.delete("all")
            except Exception:
                return
        self._countdown_after_id = self.root.after(1000, self.update_countdown)

    def draw_gradient(self):
        # Draw a vertical gradient from black to dark blue
        w = 900
        h = 400
        for i in range(h):
            r = 16
            g = 16
            b = 32 + int(48 * (i / h))  # from #101020 to #101060
            color = f'#{r:02x}{g:02x}{b:02x}'
            self.bg_canvas.create_line(0, i, w, i, fill=color)

    def get_latest_radar_times(self):
        try:
            r = requests.get(RAINVIEWER_API, timeout=10)
            r.raise_for_status()
            data = r.json()
            # Use all available frames
            times = [str(t["time"]) for t in data["radar"]["past"]]
            return times
        except Exception as e:
            print(f"Error fetching RainViewer radar times: {e}")
            return []

    def _extend_with_cached_frames(self, times):
        """Prepend older cached frame timestamps (if present on disk) so startup animation
        can show more than the API's returned recent frames (often ~13). We look into each
        location cache folder, gather timestamp file names (< earliest API time), merge,
        and keep up to EXTENDED_MAX_FRAMES total (oldest trimmed)."""
        if not times:
            return times
        try:
            earliest = int(times[0])  # RainViewer returns ascending past frames
        except Exception:
            return times
        cached_ts = set()
        for loc in LOCATIONS:
            safe_loc = ''.join(c if c.isalnum() else '_' for c in loc['name']).lower()
            cache_dir = os.path.join(BASE_DIR, 'radar_cache', safe_loc)
            if not os.path.isdir(cache_dir):
                continue
            try:
                for f in os.listdir(cache_dir):
                    if f.endswith('.png'):
                        name = f.rsplit('.',1)[0]
                        if name.isdigit():
                            val = int(name)
                            if val < earliest:  # strictly older
                                cached_ts.add(val)
            except Exception:
                pass
        if not cached_ts:
            return times
        merged = sorted(cached_ts) + [int(t) for t in times]
        # Trim to EXTENDED_MAX_FRAMES from the end (most recent EXTENDED_MAX_FRAMES)
        if len(merged) > EXTENDED_MAX_FRAMES:
            merged = merged[-EXTENDED_MAX_FRAMES:]
        # Convert back to str
        out = [str(t) for t in merged]
        if out != times:
            try:
                print(f"[Startup] Extended frames using cache: {len(times)} -> {len(out)}")
            except Exception:
                pass
        return out

    def update_loading(self, location_name, loaded, total):
        # Ensure UI updates happen on main thread
        def do_update():
            if not getattr(self, 'loading_label', None) or not self.loading_label.winfo_exists():
                return
            self.loading_status[location_name] = loaded
            status_text = " | ".join(
                [f"{name}: {self.loading_status.get(name,0)}/{len(self.radar_times)}" for name in self.loading_status.keys()]
            )
            try:
                self.loading_label.config(text=f"Loading radar images... {status_text}")
            except Exception:
                pass
        self.root.after(0, do_update)

    def check_loading_complete(self):
        if all(count == len(self.radar_times) for count in self.loading_status.values()):
            self.loading_label.config(text="Radar images loaded!")
            # Prevent duplicate animation loops (after refresh we may already be animating)
            if hasattr(self, '_anim_after_id') and self._anim_after_id:
                try:
                    self.root.after_cancel(self._anim_after_id)
                except Exception:
                    pass
                self._anim_after_id = None
            # Start / restart animation fresh
            self.frame_index = min(self.frame_index, max(0, len(self.radar_times)-1))
            self.animate()
            # Schedule slide-up removal after 20 seconds (only once)
            if not hasattr(self, '_loading_label_slide_scheduled'):
                self._loading_label_slide_scheduled = True
                self.root.after(20000, self.start_loading_label_slide)
        else:
            self.root.after(100, self.check_loading_complete)

    def animate(self):
        if self.is_paused or getattr(self, 'closing', False):
            return
        # Only proceed if all panels have at least one loaded frame
        if not (self.panels and self.radar_times and all(p.composite_images_tk for p in self.panels)):
            # Retry shortly until frames available (avoid tight loop)
            self._anim_after_id = self.root.after(250, self.animate)
            return
        # Clamp frame_index if radar_times changed
        if self.frame_index >= len(self.radar_times):
            self.frame_index = 0
        # Show current frame on each panel (guarded internally)
        for panel in self.panels:
            panel.show_frame(self.frame_index)
        # Acquire timestamp safely
        if self.frame_index < len(self.radar_times):
            timestamp = self.radar_times[self.frame_index]
        else:
            timestamp = None
        # Format timestamp label
        if timestamp is not None:
            try:
                dt = datetime.fromtimestamp(int(timestamp), UTC)
                time_str = dt.strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                time_str = str(timestamp)
        else:
            time_str = "--"
        self.timestamp_label.config(
            text=f"Frame {self.frame_index + 1}/{len(self.radar_times)} | Radar Time: {time_str}"
        )
        # Update progress bar
        self.update_progress_bar()
        # Decide delay & advance frame: pause whenever displaying CURRENT newest timestamp
        pause = False
        try:
            if timestamp is not None and timestamp == self.radar_times[-1]:
                pause = True
        except Exception:
            pass
        if pause:
            delay = 3000  # pause on newest frame
            # Wrap after pause
            self.frame_index = 0 if len(self.radar_times) > 0 else 0
        else:
            delay = ANIMATION_DELAY
            self.frame_index = (self.frame_index + 1) % max(1, len(self.radar_times))
        self._anim_after_id = self.root.after(delay, self.animate)

    def toggle_pause(self):
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.pause_btn.config(text="▶ Play")
            # Cancel scheduled animation if running
            if hasattr(self, '_anim_after_id') and self._anim_after_id:
                try:
                    self.root.after_cancel(self._anim_after_id)
                except Exception:
                    pass
                self._anim_after_id = None
        else:
            self.pause_btn.config(text="⏸ Pause")
            self.animate()

    def step_prev(self):
        if not self.radar_times:
            return
        self.is_paused = True
        self.pause_btn.config(text="▶ Play")
        self.frame_index = (self.frame_index - 1) % len(self.radar_times)
        for panel in self.panels:
            panel.show_frame(self.frame_index)
        self.update_progress_bar()
        timestamp = self.radar_times[self.frame_index]
        try:
            dt = datetime.fromtimestamp(int(timestamp), UTC)
            time_str = dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            time_str = str(timestamp)
        self.timestamp_label.config(
            text=f"Frame {self.frame_index + 1}/{len(self.radar_times)} | Radar Time: {time_str}"
        )

    def step_next(self):
        if not self.radar_times:
            return
        self.is_paused = True
        self.pause_btn.config(text="▶ Play")
        self.frame_index = (self.frame_index + 1) % len(self.radar_times)
        for panel in self.panels:
            panel.show_frame(self.frame_index)
        self.update_progress_bar()
        timestamp = self.radar_times[self.frame_index]
        try:
            dt = datetime.fromtimestamp(int(timestamp), UTC)
            time_str = dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            time_str = str(timestamp)
        self.timestamp_label.config(
            text=f"Frame {self.frame_index + 1}/{len(self.radar_times)} | Radar Time: {time_str}"
        )

    def update_progress_bar(self):
        self.progress_canvas.delete("all")
        width = self.progress_canvas.winfo_width()
        height = self.progress_canvas.winfo_height()
        if width < 10: width = 300  # fallback for initial size
        bar_margin = 4
        bar_width = width - 2 * bar_margin
        bar_height = height - 2 * bar_margin
        progress = (self.frame_index + 1) / len(self.radar_times)
        fill_width = int(bar_width * progress)
        # Draw background bar
        self.progress_canvas.create_rectangle(bar_margin, bar_margin, bar_margin + bar_width, bar_margin + bar_height, fill="#333", outline="#666")
        # Draw progress
        self.progress_canvas.create_rectangle(bar_margin, bar_margin, bar_margin + fill_width, bar_margin + bar_height, fill="#00ff99", outline="")
        # Draw slider/knob
        knob_x = bar_margin + fill_width
        self.progress_canvas.create_oval(knob_x-8, bar_margin, knob_x+8, bar_margin+bar_height, fill="#00ff99", outline="#00cc66")

    def get_panel_size(self):
        # Calculate panel size based on window size
        total_width = self.root.winfo_width()
        total_height = self.root.winfo_height() - 60  # subtract label heights
        panel_width = max(128, int(total_width / len(LOCATIONS)))
        panel_height = max(128, int(total_height))
        self.panel_width = panel_width
        self.panel_height = panel_height
        return panel_width, panel_height

    def on_resize(self, event):
        # Redraw gradient background to fit new window size
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        self.bg_canvas.config(width=w, height=h)
        self.bg_canvas.delete("all")
        for i in range(h):
            r = 16
            g = 16
            b = 32 + int(48 * (i / max(h, 1)))
            color = f'#{r:02x}{g:02x}{b:02x}'
            self.bg_canvas.create_line(0, i, w, i, fill=color)
        # Do not update scaled images on resize
        for panel in self.panels:
            try:
                panel.show_frame(self.frame_index)
            except Exception:
                pass

    def refresh_radar_frames(self):
        # Fetch latest radar times
        try:
            r = requests.get(RAINVIEWER_API, timeout=10)
            r.raise_for_status()
            data = r.json()
            new_times = [str(t["time"]) for t in data["radar"]["past"]]
        except Exception as e:
            print(f"Error refreshing RainViewer radar times: {e}")
            # Fallback schedule
            self.schedule_next_refresh(fallback=True)
            return

        # Only update if there are new frames
        if new_times:
            existing_set = set(self.radar_times)
            appended = False
            for ts in new_times:
                if ts not in existing_set:
                    self.radar_times.append(ts)
                    appended = True
            if appended:
                # Log newly added frames
                import datetime as _dt
                for ts in new_times:
                    if ts not in existing_set:
                        try:
                            dt = _dt.datetime.fromtimestamp(int(ts), UTC)
                            printable = dt.strftime('%Y-%m-%d %H:%M:%S UTC')
                        except Exception:
                            printable = ts
                        print(f"[Radar] New frame acquired: {printable}")
                # Trim to extended max frames
                if len(self.radar_times) > EXTENDED_MAX_FRAMES:
                    # Drop oldest
                    overflow = len(self.radar_times) - EXTENDED_MAX_FRAMES
                    self.radar_times = self.radar_times[overflow:]
                # Reset load status and reload frames
                self.loading_status = {loc["name"]: 0 for loc in LOCATIONS}
                for panel in self.panels:
                    panel.radar_times = self.radar_times
                    threading.Thread(target=panel.load_radar_frames, daemon=True).start()
                self.frame_index = 0
                self.show_loading_message("Updating radar images...", slide_after=10000)
                self.check_loading_complete()
        # Schedule next predictive refresh
        self.schedule_next_refresh()

    # ----------------- Loading label slide-up animation -----------------
    def start_loading_label_slide(self):
        # Only proceed if label still exists and was packed
        if getattr(self, 'closing', False) or not self.loading_label.winfo_exists():
            return
        try:
            # Convert from pack to place so we can animate y
            self.loading_label.update_idletasks()
            y = self.loading_label.winfo_y()
            h = self.loading_label.winfo_height()
            # Remove from pack and place at same spot
            self.loading_label.pack_forget()
            self.loading_label.place(x=0, y=y, relwidth=1)
            self._loading_label_slide_y = y
            self._loading_label_height = h
            self.animate_loading_label_slide()
        except Exception:
            # Fallback: just hide
            try:
                self.loading_label.destroy()
            except Exception:
                pass

    def animate_loading_label_slide(self):
        if getattr(self, 'closing', False) or not self.loading_label.winfo_exists():
            return
        self._loading_label_slide_y -= 4  # pixels per frame
        self.loading_label.place_configure(y=self._loading_label_slide_y)
        if self._loading_label_slide_y + self._loading_label_height <= 0:
            try:
                self.loading_label.destroy()
            except Exception:
                pass
            return
        # Schedule next frame ~16ms (~60fps)
        if not getattr(self, 'closing', False):
            self._loading_slide_after_id = self.root.after(16, self.animate_loading_label_slide)

    # ----------------- Utility to (re)show loading banner -----------------
    def show_loading_message(self, text, slide_after=None):
        if getattr(self, 'closing', False):
            return
        # Recreate label if missing (e.g., after it was destroyed)
        if not getattr(self, 'loading_label', None) or not self.loading_label.winfo_exists():
            self.loading_label = tk.Label(self.root, text=text, font=("Arial", 14), bg="black", fg="yellow")
            # Insert just below progress canvas (pack before frame)
            self.loading_label.pack(side="top", fill="x", pady=5)
            # Reset slide flag so we can slide again
            if hasattr(self, '_loading_label_slide_scheduled'):
                delattr(self, '_loading_label_slide_scheduled')
        else:
            try:
                self.loading_label.config(text=text)
            except Exception:
                return
        if slide_after is not None:
            if getattr(self, '_pending_slide_after_id', None):
                try:
                    self.root.after_cancel(self._pending_slide_after_id)
                except Exception:
                    pass
            def schedule():
                if not hasattr(self, '_loading_label_slide_scheduled'):
                    self._loading_label_slide_scheduled = True
                    self.start_loading_label_slide()
            if not getattr(self, 'closing', False):
                self._pending_slide_after_id = self.root.after(slide_after, schedule)

    # ----------------- Predictive scheduling -----------------
    def schedule_next_refresh(self, initial=False, fallback=False):
        if getattr(self, 'closing', False):
            return
        # Cancel previous scheduled refresh if any
        if getattr(self, '_refresh_after_id', None):
            try:
                self.root.after_cancel(self._refresh_after_id)
            except Exception:
                pass
            self._refresh_after_id = None
        now = time.time()
        delay_ms = REFRESH_INTERVAL  # default fallback
        if not fallback and self.radar_times and len(self.radar_times) >= 2:
            try:
                last = int(self.radar_times[-1])
                prev = int(self.radar_times[-2])
                interval = max(60, min(900, last - prev))  # clamp 1–15 min
                expected_next = last + interval
                target_time = expected_next + (DELAY_AFTER_FRAME_AVAILABLE_MS/1000.0)
                delay_sec = target_time - now
                if delay_sec < 5:
                    delay_sec = 5  # minimum wait
                delay_ms = int(delay_sec * 1000)
                print(f"[Scheduler] Last frames {prev}->{last} (Δ={interval}s). Next expected ~{interval}s later. Scheduling refresh in {delay_sec:.1f}s (includes 30s delay).")
            except Exception as e:
                print(f"[Scheduler] Predictive scheduling failed: {e}; using fallback {REFRESH_INTERVAL/1000:.0f}s.")
        else:
            if fallback:
                print(f"[Scheduler] Fallback scheduling in {REFRESH_INTERVAL/1000:.0f}s due to fetch error.")
        self.next_refresh_timestamp = now + (delay_ms/1000.0)
        # Store total countdown seconds for progress bar
        self._countdown_total = int(delay_ms/1000.0)
        if not getattr(self, 'closing', False):
            self._refresh_after_id = self.root.after(delay_ms, self.refresh_radar_frames)
        if initial:
            print(f"[Scheduler] Initial radar refresh scheduled in {delay_ms/1000:.1f}s.")

if __name__ == "__main__":
    root = tk.Tk()
    app = RadarApp(root)
    try:
        root.lift()
        root.attributes('-topmost', True)
        root.after(100, lambda: root.attributes('-topmost', False))
    except Exception:
        pass
    root.mainloop()