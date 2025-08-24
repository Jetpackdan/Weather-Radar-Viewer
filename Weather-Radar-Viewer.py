import requests
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from PIL import Image, ImageTk
import io
import os
import threading

RAINVIEWER_API = "https://api.rainviewer.com/public/weather-maps.json"
TILE_URL_TEMPLATE = "https://tilecache.rainviewer.com/v2/radar/{time}/256/{z}/{lat}/{lon}/2/1_1.png"
GOOGLE_MAPS_TEMPLATE = (
    "https://maps.googleapis.com/maps/api/staticmap?"
    "center={lat},{lon}&zoom={z}&size=256x256&maptype=roadmap&key={api_key}"
)
GOOGLE_MAPS_API_KEY = "AIzaSyD3oN5YeEhwEQvmKkN0fNY-EHm6uBa11Qk"

LOCATIONS = [
    {"name": "Miami, FL", "lat": 25.7617, "lon": -80.1918},
    {"name": "Portland, OR", "lat": 45.5152, "lon": -122.6784}
]


REFRESH_INTERVAL = 300000  # 5 minutes in milliseconds
ANIMATION_DELAY = 120      # ms between frames
MAX_FRAMES = 100
RADAR_THREAD_POOL = ThreadPoolExecutor(max_workers=4)


# Forecast cache
_forecast_cache = {}
def get_hourly_forecast(lat, lon):
    """Return structured 12-hour local forecast.
    Structure: {
       'timezone': <IANA tz name>,
       'entries': [ { 'time': datetime (tz-aware), 'temp': float, 'weather': str } ...]
    }
    On error returns {'timezone': 'UTC', 'entries': []}.
    """
    key = (lat, lon)
    if key in _forecast_cache:
        return _forecast_cache[key]
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}&hourly=temperature_2m,weathercode&forecast_days=1&timezone=auto"
    )
    try:
        r = requests_session.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo  # Python 3.9+
        except Exception:
            ZoneInfo = None
        tz_name = data.get("timezone", "UTC")
        temps = data["hourly"]["temperature_2m"][:12]
        codes = data["hourly"]["weathercode"][:12]
        times = data["hourly"]["time"][:12]  # already localized textual times per open-meteo
        code_map = {
            0: "Clear", 1: "Mainly Clear", 2: "Partly Cloudy", 3: "Overcast",
            45: "Fog", 48: "Depositing Rime Fog", 51: "Drizzle", 61: "Rain",
            71: "Snow", 80: "Rain Showers", 95: "Thunderstorm"
        }
        entries = []
        for t, temp, c in zip(times, temps, codes):
            # t format: YYYY-MM-DDTHH:MM (local time per timezone=auto)
            try:
                dt_naive = datetime.strptime(t, "%Y-%m-%dT%H:%M")
                if ZoneInfo:
                    dt = dt_naive.replace(tzinfo=ZoneInfo(tz_name))
                else:
                    dt = dt_naive  # fallback naive
            except Exception:
                dt = t  # keep raw string if parse fails
            entries.append({
                'time': dt,
                'temp': temp,
                'weather': code_map.get(c, str(c))
            })
        result = {"timezone": tz_name, "entries": entries}
        _forecast_cache[key] = result
        return result
    except Exception as e:
        print(f"Error fetching forecast for {lat},{lon}: {e}")
        return {"timezone": "UTC", "entries": []}

class RadarPanel:
    def __init__(self, parent, location, radar_times, loading_callback, get_size_callback):
        self.location = location
        self.radar_times = radar_times
        self.loading_callback = loading_callback
        self.get_size_callback = get_size_callback
        self.panel_frame = tk.Frame(parent, bg="black")
        self.panel_frame.pack(side="left", fill="both", expand=True)
        self.label = tk.Label(self.panel_frame, bg="black", text=location["name"], compound="top", fg="white", font=("Arial", 14))
        self.label.pack(fill="x", expand=False)
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
        self.composite_images_pil.clear()
        self.composite_images_tk.clear()
        z = 8
        headers = {"User-Agent": "Mozilla/5.0"}
        if not self.map_image_pil:
            return
        total_count = len(self.radar_times)

        def load_one_frame(radar_time):
            radar_url = TILE_URL_TEMPLATE.format(
                time=radar_time,
                z=z,
                lat=self.location["lat"],
                lon=self.location["lon"]
            )
            try:
                r_radar = requests_session.get(radar_url, timeout=10, headers=headers)
                r_radar.raise_for_status()
                radar_img = Image.open(io.BytesIO(r_radar.content)).convert("RGBA")
                alpha = radar_img.split()[-1].point(lambda p: int(p * 0.7))
                radar_img.putalpha(alpha)
                composite = Image.alpha_composite(self.map_image_pil, radar_img)
                return composite
            except Exception as e:
                print(f"Error loading radar for {self.location['name']} at {radar_time}: {e}")
                return self.map_image_pil.copy()

        results = list(RADAR_THREAD_POOL.map(load_one_frame, self.radar_times))
        self.composite_images_pil.extend(results)
        for idx in range(total_count):
            self.loading_callback(self.location["name"], idx + 1, total_count)
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
        icon_map = {
            "Clear": "sun.png",
            "Mainly Clear": "sun_cloud.png",
            "Partly Cloudy": "cloud.png",
            "Overcast": "cloud.png",
            "Fog": "fog.png",
            "Depositing Rime Fog": "fog.png",
            "Drizzle": "drizzle.png",
            "Rain": "rain.png",
            "Snow": "snow.png",
            "Rain Showers": "rain.png",
            "Thunderstorm": "storm.png"
        }
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

        # Use current local hour as starting label, advance by one hour for each entry
        from datetime import datetime, timedelta
        if entries:
            first_dt = entries[0]["time"]
            if hasattr(first_dt, 'tzinfo') and getattr(first_dt, 'tzinfo', None) is not None:
                now_local = datetime.now(first_dt.tzinfo)
            else:
                now_local = datetime.now()
            base_hour = now_local.replace(minute=0, second=0, microsecond=0)
        else:
            base_hour = None

        last_day = None
        for idx, entry in enumerate(entries):
            temp = entry["temp"]
            weather = entry["weather"]
            # Synthetic display time based on current hour + idx hours
            if base_hour:
                display_dt = base_hour + timedelta(hours=idx)
                day = display_dt.day
                hour_str = display_dt.strftime('%I%p').lstrip('0')
            else:
                dt_obj = entry["time"]
                if hasattr(dt_obj, 'strftime'):
                    day = dt_obj.day
                    hour_str = dt_obj.strftime('%I%p').lstrip('0')
                else:
                    hour_str = str(dt_obj)
                    day = None

            # Day change separator
            if last_day is not None and day is not None and day != last_day:
                sep = tk.Frame(row_frame, width=6, bg="black")
                sep.pack(side="left")
                tk.Label(row_frame, text="|", fg="yellow", bg="black").pack(side="left", padx=(0,2))
            last_day = day if day is not None else last_day

            icon_file = icon_map.get(weather, "unknown.png")
            icon_path = os.path.join("weather_icons", icon_file)
            try:
                icon_img = Image.open(icon_path).resize((30, 30), Image.LANCZOS)
                icon_tk = ImageTk.PhotoImage(icon_img)
            except Exception:
                icon_tk = None
            cell = tk.Frame(row_frame, bg="black")
            cell.pack(side="left", padx=2)
            if icon_tk:
                lbl_icon = tk.Label(cell, image=icon_tk, bg="black")
                lbl_icon.image = icon_tk
                lbl_icon.pack(side="top")
            tk.Label(cell, text=f"{hour_str}\n{temp:.0f}°C", fg="white", bg="black", font=("Arial", 9)).pack(side="top")

        # Tooltip-ish legend (simple text) for start time
        try:
            start_dt = entries[0]["time"]
            if hasattr(start_dt, 'strftime'):
                start_str = start_dt.strftime('%a %I:%M %p').lstrip('0')
            else:
                start_str = str(start_dt)
            legend = tk.Label(self.forecast_frame, text=f"Starting {start_str}", fg="#888", bg="black", font=("Arial", 8))
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
        # Progress bar canvas
        self.progress_canvas = tk.Canvas(root, height=20, bg="black", highlightthickness=0)
        self.progress_canvas.pack(side="top", fill="x", pady=2)
        # Loading screen
        self.loading_label = tk.Label(root, text="Loading radar images...", font=("Arial", 14), bg="black", fg="yellow")
        self.loading_label.pack(side="top", fill="x", pady=5)
        self.frame = tk.Frame(root, bg="black")
        self.frame.pack(fill="both", expand=True)
        # Countdown label (top right)
        self.countdown_label = tk.Label(root, text="", font=("Arial", 12), bg="black", fg="cyan")
        self.countdown_label.place(relx=1.0, rely=0.0, anchor="ne", x=-10, y=10)
        self.radar_times = self.get_latest_radar_times()
        self.loading_status = {loc["name"]: 0 for loc in LOCATIONS}
        self.panel_width = 256
        self.panel_height = 256
        self.panels = [
            RadarPanel(self.frame, LOCATIONS[0], self.radar_times, self.update_loading, self.get_panel_size),
            RadarPanel(self.frame, LOCATIONS[1], self.radar_times, self.update_loading, self.get_panel_size)
        ]
        self.frame_index = 0
        self.next_refresh_seconds = REFRESH_INTERVAL // 1000
        self.root.bind("<Escape>", lambda e: self.root.destroy())
        self.root.bind("<Configure>", self.on_resize)
        self.check_loading_complete()
        self.root.after(REFRESH_INTERVAL, self.refresh_radar_frames)
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
        self.loading_status[location_name] = loaded
        status_text = " | ".join(
            [f"{name}: {self.loading_status[name]}/{total}" for name, total in zip(self.loading_status.keys(), [len(self.radar_times)]*len(LOCATIONS))]
        )
        self.loading_label.config(text=f"Loading radar images... {status_text}")

    def check_loading_complete(self):
        if all(count == len(self.radar_times) for count in self.loading_status.values()):
            self.loading_label.config(text="Radar images loaded!")
            self.animate()
        else:
            self.root.after(100, self.check_loading_complete)

    def animate(self):
        if self.panels and self.radar_times:
            self.frame_index = (self.frame_index + 1) % len(self.radar_times)
            for panel in self.panels:
                panel.show_frame(self.frame_index)
            # Update timestamp label
            timestamp = self.radar_times[self.frame_index]
            import datetime
            try:
                dt = datetime.datetime.utcfromtimestamp(int(timestamp))
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
        else:
            delay = int(ANIMATION_DELAY * 1.3)  # Slow down by 30%
        self.root.after(delay, self.animate)

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
            self.next_refresh_seconds = REFRESH_INTERVAL // 1000
            self.root.after(REFRESH_INTERVAL, self.refresh_radar_frames)
            return

        # Only update if there are new frames
        if new_times != self.radar_times:
            # Keep only the last 100 frames
            self.radar_times = new_times[-MAX_FRAMES:]
            self.loading_status = {loc["name"]: 0 for loc in LOCATIONS}
            # Reload radar frames for each panel in a background thread
            for panel in self.panels:
                panel.radar_times = self.radar_times
                threading.Thread(target=panel.load_radar_frames, daemon=True).start()
            # Reset animation index if needed
            self.frame_index = 0
            self.loading_label.config(text="Updating radar images...")
            self.check_loading_complete()
        self.next_refresh_seconds = REFRESH_INTERVAL // 1000
        self.root.after(REFRESH_INTERVAL, self.refresh_radar_frames)

    def update_countdown(self):
        if self.next_refresh_seconds > 0:
            self.countdown_label.config(text=f"Next radar update in {self.next_refresh_seconds}s")
            self.next_refresh_seconds -= 1
        else:
            self.countdown_label.config(text="Updating radar images...")
        self.root.after(1000, self.update_countdown)

if __name__ == "__main__":
    root = tk.Tk()
    app = RadarApp(root)
    root.mainloop()