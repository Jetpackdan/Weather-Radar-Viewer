import requests
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from PIL import Image, ImageTk, ImageDraw
import io
import os
import threading
import time
from datetime import datetime, UTC

RAINVIEWER_API = "https://api.rainviewer.com/public/weather-maps.json"
TILE_URL_TEMPLATE = "https://tilecache.rainviewer.com/v2/radar/{time}/256/{z}/{lat}/{lon}/2/1_1.png"
GOOGLE_MAPS_TEMPLATE = (
    "https://maps.googleapis.com/maps/api/staticmap?"
    "center={lat},{lon}&zoom={z}&size=256x256&maptype=roadmap&key={api_key}"
)
GOOGLE_MAPS_API_KEY = "AIzaSyD3oN5YeEhwEQvmKkN0fNY-EHm6uBa11Qk"

LOCATIONS = [
    {"name": "Miami, FL", "lat": 25.7617, "lon": -80.1918},
    {"name": "Klamath Falls, OR", "lat": 42.2249, "lon": -121.7817}
]


REFRESH_INTERVAL = 30000   # Fallback poll interval (ms)
DELAY_AFTER_FRAME_AVAILABLE_MS = 30000  # Wait 30s after expected new frame time before fetching
ANIMATION_DELAY = 120      # ms between frames
MAX_FRAMES = 100
RADAR_THREAD_POOL = ThreadPoolExecutor(max_workers=4)
EXTENDED_MAX_FRAMES = 50  # Number of frames to retain locally for extended animation


# Forecast cache
_forecast_cache = {}
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
                dt = datetime.fromisoformat(p["startTime"].replace("Z", "+00:00"))
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
        self.panel_frame = tk.Frame(parent, bg="black")
        self.panel_frame.pack(side="left", fill="both", expand=True)
        self.label = tk.Label(self.panel_frame, bg="black", text=location["name"], fg="white", font=("Arial", 14))
        self.label.pack(fill="x", expand=False)
        self.show_alerts_var = tk.BooleanVar(value=False)
        self.alerts_checkbox = None
        self.alerts_frame = tk.Frame(self.panel_frame, bg="black")
        self.alerts_frame.pack(fill="x", expand=False)
        self.composite_image_label = tk.Label(self.panel_frame, bg="black")
        self.composite_image_label.pack(fill="both", expand=True)
        self.composite_images_pil = []
        self.composite_images_tk = []
        self.frame_index = 0
        self.map_image_pil = None
        # Forecast area (bottom)
        self.forecast_frame = tk.Frame(self.panel_frame, bg="black")
        self.forecast_frame.pack(side="bottom", fill="x", pady=(4, 0))
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
        except Exception as e:
            print(f"Error loading Google map for {self.location['name']}: {e}")
            self.map_image_pil = None

    def load_radar_frames(self):
        """Load radar frames, using local cache for extended animation length.

        Composite images are cached on disk under radar_cache/<location>/ <timestamp>.png
        We attempt to reuse existing composites for older frames beyond API-provided list.
        """
        self.composite_images_pil.clear()
        self.composite_images_tk.clear()
        if not self.map_image_pil:
            return
        z = 8
        headers = {"User-Agent": "Mozilla/5.0"}
        # Prepare cache directory per location
        safe_loc = ''.join(c if c.isalnum() else '_' for c in self.location['name']).lower()
        cache_dir = os.path.join('radar_cache', safe_loc)
        os.makedirs(cache_dir, exist_ok=True)

        def cache_path(ts):
            return os.path.join(cache_dir, f"{ts}.png")

        total_count = len(self.radar_times)

        def load_or_fetch(ts):
            path = cache_path(ts)
            if os.path.exists(path):
                try:
                    return Image.open(path).convert("RGBA")
                except Exception:
                    pass  # fall through to refetch
            # Attempt fetch (may fail for very old frames that RainViewer no longer serves)
            radar_url = TILE_URL_TEMPLATE.format(
                time=ts,
                z=z,
                lat=self.location["lat"],
                lon=self.location["lon"]
            )
            try:
                r_radar = requests_session.get(radar_url, timeout=10, headers=headers)
                r_radar.raise_for_status()
                radar_img = Image.open(io.BytesIO(r_radar.content)).convert("RGBA")
                # Semi-transparent radar overlay
                alpha = radar_img.split()[-1].point(lambda p: int(p * 0.7))
                radar_img.putalpha(alpha)
                composite = Image.alpha_composite(self.map_image_pil, radar_img)
                # Save composite to cache
                try:
                    composite.save(path, format='PNG')
                except Exception as e:
                    print(f"Cache save failed {path}: {e}")
                return composite
            except Exception as e:
                # If fetch fails, try existing cached file again or fall back to map
                if os.path.exists(path):
                    try:
                        return Image.open(path).convert("RGBA")
                    except Exception:
                        pass
                print(f"Radar fetch failed for {self.location['name']} {ts}: {e}")
                return self.map_image_pil.copy()

        # Sequentially load to respect ordering; could be parallelized but 50 small PNGs is fine
        for idx, ts in enumerate(self.radar_times):
            img = load_or_fetch(ts)
            self.composite_images_pil.append(img)
            self.loading_callback(self.location["name"], idx + 1, total_count)

        # Trim on-disk cache to EXTENDED_MAX_FRAMES (oldest first)
        try:
            cached_files = [f for f in os.listdir(cache_dir) if f.endswith('.png')]
            # Extract timestamps that are numeric
            parsed = []
            for f in cached_files:
                name = f.rsplit('.', 1)[0]
                if name.isdigit():
                    parsed.append(int(name))
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
        if self.composite_images_tk and getattr(self, '_last_frame', None) != frame_index:
            self.composite_image_label.config(image=self.composite_images_tk[frame_index])
            self._last_frame = frame_index

    # ----------------- Forecast Rendering -----------------
    def update_forecast(self):
        data = get_hourly_forecast(self.location["lat"], self.location["lon"])
        tz_name = data.get("timezone", "") if isinstance(data, dict) else ""
        entries = data.get("entries", []) if isinstance(data, dict) else []
        # Clear previous
        for w in self.forecast_frame.winfo_children():
            w.destroy()

        if not entries:
            tk.Label(self.forecast_frame, text="Forecast unavailable", fg="white", bg="black", font=("Arial", 10)).pack(side="left")
            return

        # Header with timezone (short form from any entry if possible)
        from datetime import datetime
        try:
            first_dt = entries[0]["time"]
            if hasattr(first_dt, 'strftime'):
                tz_abbr = first_dt.strftime('%Z') or tz_name.split('/')[-1]
            else:
                tz_abbr = tz_name.split('/')[-1]
        except Exception:
            tz_abbr = tz_name.split('/')[-1] if tz_name else ""

        header = tk.Label(self.forecast_frame, text=f"Next 12 hrs ({tz_abbr})", fg="cyan", bg="black", font=("Arial", 10, 'bold'))
        header.pack(side="top", anchor="w")

        row_frame = tk.Frame(self.forecast_frame, bg="black")
        row_frame.pack(side="top", fill="x")

        last_day = None
        for entry in entries:
            dt_obj = entry["time"]
            temp = entry["temp"]
            weather = entry["weather"]
            pop = entry.get("pop")
            wind_speed = entry.get("wind_speed")
            wind_dir = entry.get("wind_dir")
            if hasattr(dt_obj, 'strftime'):
                day = dt_obj.day
                hour_str = dt_obj.strftime('%I%p').lstrip('0')
            else:
                try:
                    hour_str = str(dt_obj).split('T')[1][:5]
                except Exception:
                    hour_str = str(dt_obj)
                day = None

            # Day change separator
            if last_day is not None and day is not None and day != last_day:
                sep = tk.Frame(row_frame, width=6, bg="black")
                sep.pack(side="left")
                tk.Label(row_frame, text="|", fg="yellow", bg="black").pack(side="left", padx=(0,2))
            last_day = day if day is not None else last_day

            cell = tk.Frame(row_frame, bg="black")
            cell.pack(side="left", padx=2)
            extra = f"\n{int(pop)}%" if isinstance(pop, (int, float)) else ""
            wind_txt = ""
            if wind_speed is not None and wind_dir is not None:
                # Wind direction as arrow and degrees
                arrow = "↑"
                deg = int(wind_dir)
                arrows = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"]
                idx = int(((deg + 22.5) % 360) // 45)
                arrow = arrows[idx]
                wind_txt = f"\n{arrow} {wind_speed:.1f}m/s"
            # Truncate weather description to max 16 chars
            weather_short = weather if len(str(weather)) <= 16 else str(weather)[:13] + "..."
            tk.Label(cell, text=f"{hour_str}\n{temp:.0f}°C\n{weather_short}{extra}{wind_txt}", fg="white", bg="black", font=("Arial", 9), wraplength=60, justify="center").pack(side="top")

        # Tooltip-ish legend (simple text) for start time
        try:
            start_dt = entries[0]["time"]
            end_dt = entries[-1]["time"]
            if hasattr(start_dt, 'strftime') and hasattr(end_dt, 'strftime'):
                start_str = start_dt.strftime('%a %I:%M %p').lstrip('0')
                end_str = end_dt.strftime('%I:%M %p').lstrip('0')
                legend_text = f"Covers {start_str} to {end_str}"
            else:
                legend_text = f"Starting {start_dt}"
            legend = tk.Label(self.forecast_frame, text=legend_text, fg="#888", bg="black", font=("Arial", 8))
            legend.pack(side="top", anchor="w")
        except Exception:
            pass

requests_session = requests.Session()


class RadarApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Oregon Radar: Portland & Klamath Falls (Animated Layered Radar + Google Maps)")
        self.root.geometry("900x400")

        # Timestamp label at top center
        self.timestamp_label = tk.Label(root, text="", font=("Arial", 16), bg="black", fg="white")
        self.timestamp_label.pack(side="top", fill="x", pady=2)

        # Animation controls frame
        controls_frame = tk.Frame(root, bg="black")
        controls_frame.pack(side="top", fill="x", pady=2)
        self.is_paused = False
        self.pause_btn = tk.Button(controls_frame, text="⏸ Pause", font=("Arial", 10), command=self.toggle_pause, bg="#222", fg="white")
        self.pause_btn.pack(side="left", padx=4)
        self.prev_btn = tk.Button(controls_frame, text="−", font=("Arial", 12, "bold"), width=2, command=self.step_prev, bg="#222", fg="white")
        self.prev_btn.pack(side="left", padx=2)
        self.next_btn = tk.Button(controls_frame, text="+", font=("Arial", 12, "bold"), width=2, command=self.step_next, bg="#222", fg="white")
        self.next_btn.pack(side="left", padx=2)

        # Progress bar canvas
        self.progress_canvas = tk.Canvas(root, height=20, bg="black", highlightthickness=0)
        self.progress_canvas.pack(side="top", fill="x", pady=2)

        # Loading screen
        self.loading_label = tk.Label(root, text="Loading radar images...", font=("Arial", 14), bg="black", fg="yellow")
        self.loading_label.pack(side="top", fill="x", pady=5)

        # Container frame for radar panels
        self.frame = tk.Frame(root, bg="black")
        self.frame.pack(fill="both", expand=True)

        # Countdown label (top right)
        self.countdown_label = tk.Label(root, text="", font=("Arial", 12), bg="black", fg="cyan")
        self.countdown_label.place(relx=1.0, rely=0.0, anchor="ne", x=-10, y=10)

        # Initial radar times
        self.radar_times = self.get_latest_radar_times()
        self.loading_status = {loc["name"]: 0 for loc in LOCATIONS}

        # Panel sizing
        self.panel_width = 256
        self.panel_height = 256

        # Create radar panels
        self.panels = [
            RadarPanel(self.frame, LOCATIONS[0], self.radar_times, self.update_loading, self.get_panel_size),
            RadarPanel(self.frame, LOCATIONS[1], self.radar_times, self.update_loading, self.get_panel_size)
        ]

        self.frame_index = 0

        # Key bindings
        self.root.bind("<Escape>", lambda e: self.root.destroy())
        self.root.bind("<Configure>", self.on_resize)

        # Start loading check
        self.check_loading_complete()

        # Predictive scheduling: will align refresh with expected next frame time + delay
        self._refresh_after_id = None
        self.next_refresh_timestamp = time.time() + (REFRESH_INTERVAL/1000)
        self.schedule_next_refresh(initial=True)
        self.update_countdown()

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
            self.animate()
            # Schedule slide-up removal after 20 seconds (only once)
            if not hasattr(self, '_loading_label_slide_scheduled'):
                self._loading_label_slide_scheduled = True
                self.root.after(20000, self.start_loading_label_slide)
        else:
            self.root.after(100, self.check_loading_complete)

    def animate(self):
        if self.is_paused:
            return
        if self.panels and self.radar_times:
            # Clamp frame_index if radar_times changed
            if self.frame_index >= len(self.radar_times):
                self.frame_index = 0
            # Show frame
            for panel in self.panels:
                panel.show_frame(self.frame_index)
            # Update timestamp label
            timestamp = self.radar_times[self.frame_index]
            try:
                dt = datetime.fromtimestamp(int(timestamp), UTC)
                time_str = dt.strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                time_str = str(timestamp)
            self.timestamp_label.config(
                text=f"Frame {self.frame_index + 1}/{len(self.radar_times)} | Radar Time: {time_str}"
            )
            # Update animated progress bar
            self.update_progress_bar()

            # Animation delay logic
            if self.frame_index == len(self.radar_times) - 1:
                delay = 3000  # 3 seconds pause on most current frame
                self.frame_index = 0  # After pause, loop to first frame
            else:
                delay = int(ANIMATION_DELAY * 1.5)  # Slow down by 50%
                self.frame_index += 1
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
        # Do not update scaled images on resize
        for panel in self.panels:
            panel.show_frame(self.frame_index)

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

    def update_countdown(self):
        # Derive remaining seconds from timestamp
        now = time.time()
        remaining = int(self.next_refresh_timestamp - now)
        if remaining >= 0:
            self.countdown_label.config(text=f"Next radar check in {remaining}s")
        else:
            self.countdown_label.config(text="Checking for new radar frame...")
        self.root.after(1000, self.update_countdown)

    # ----------------- Loading label slide-up animation -----------------
    def start_loading_label_slide(self):
        # Only proceed if label still exists and was packed
        if not self.loading_label.winfo_exists():
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
        if not self.loading_label.winfo_exists():
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
        self.root.after(16, self.animate_loading_label_slide)

    # ----------------- Utility to (re)show loading banner -----------------
    def show_loading_message(self, text, slide_after=None):
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
            self.root.after_cancel(getattr(self, '_pending_slide_after_id', self.root.after(0, lambda: None)))
            def schedule():
                if not hasattr(self, '_loading_label_slide_scheduled'):
                    self._loading_label_slide_scheduled = True
                    self.start_loading_label_slide()
            self._pending_slide_after_id = self.root.after(slide_after, schedule)

    # ----------------- Predictive scheduling -----------------
    def schedule_next_refresh(self, initial=False, fallback=False):
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
        self._refresh_after_id = self.root.after(delay_ms, self.refresh_radar_frames)
        if initial:
            print(f"[Scheduler] Initial radar refresh scheduled in {delay_ms/1000:.1f}s.")

if __name__ == "__main__":
    root = tk.Tk()
    app = RadarApp(root)
    root.mainloop()