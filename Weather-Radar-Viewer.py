import requests
import tkinter as tk
from PIL import Image, ImageTk
import io
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
    {"name": "Klamath Falls, OR", "lat": 42.224, "lon": -121.781}
]

REFRESH_INTERVAL = 300000  # 5 minutes in milliseconds
ANIMATION_DELAY = 120      # ms between frames
MAX_FRAMES = 100

def get_hourly_forecast(lat, lon):
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}&hourly=temperature_2m,weathercode&forecast_days=1"
    )
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        temps = data["hourly"]["temperature_2m"][:12]
        codes = data["hourly"]["weathercode"][:12]
        times = data["hourly"]["time"][:12]
        # Simple weather code mapping
        code_map = {
            0: "Clear", 1: "Mainly Clear", 2: "Partly Cloudy", 3: "Overcast",
            45: "Fog", 48: "Depositing Rime Fog", 51: "Drizzle", 61: "Rain",
            71: "Snow", 80: "Rain Showers", 95: "Thunderstorm"
        }
        weather = [code_map.get(c, str(c)) for c in codes]
        return list(zip(times, temps, weather))
    except Exception as e:
        print(f"Error fetching forecast for {lat},{lon}: {e}")
        return []

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
        # Move forecast label to the bottom
        self.forecast_label = tk.Label(
            self.panel_frame, bg="black", fg="white", font=("Arial", 10),
            justify="left", anchor="w", wraplength=400
        )
        self.forecast_label.pack(side="bottom", fill="x", pady=(4, 0))
        self.load_map()
        threading.Thread(target=self.load_radar_frames, daemon=True).start()
        self.panel_frame.after(500, self.update_forecast)

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
            r_map = requests.get(map_url, timeout=10, headers=headers)
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
        loaded_count = 0
        total_count = len(self.radar_times)
        for radar_time in self.radar_times:
            radar_url = TILE_URL_TEMPLATE.format(
                time=radar_time,
                z=z,
                lat=self.location["lat"],
                lon=self.location["lon"]
            )
            try:
                r_radar = requests.get(radar_url, timeout=10, headers=headers)
                r_radar.raise_for_status()
                radar_img = Image.open(io.BytesIO(r_radar.content)).convert("RGBA")
                alpha = radar_img.split()[-1].point(lambda p: int(p * 0.7))
                radar_img.putalpha(alpha)
                composite = Image.alpha_composite(self.map_image_pil, radar_img)
                self.composite_images_pil.append(composite)
            except Exception as e:
                print(f"Error loading radar for {self.location['name']} at {radar_time}: {e}")
                self.composite_images_pil.append(self.map_image_pil.copy())
            loaded_count += 1
            self.loading_callback(self.location["name"], loaded_count, total_count)
        self.update_scaled_images()

    def update_scaled_images(self):
        # Get current size from callback
        width, height = self.get_size_callback()
        self.composite_images_tk.clear()
        for img in self.composite_images_pil:
            scaled = img.resize((width, height), Image.LANCZOS)
            self.composite_images_tk.append(ImageTk.PhotoImage(scaled))

    def show_frame(self, frame_index):
        if self.composite_images_tk:
            self.composite_image_label.config(image=self.composite_images_tk[frame_index])

    def update_forecast(self):
        forecast = get_hourly_forecast(self.location["lat"], self.location["lon"])
        if forecast:
            text = "Hourly Forecast:\n"
            for t, temp, w in forecast:
                hour = t.split("T")[1][:5]
                text += f"{hour}: {temp:.1f}°C, {w}\n"
        else:
            text = "Hourly Forecast: unavailable"
        # Update label in main thread
        self.panel_frame.after(0, lambda: self.forecast_label.config(text=text))

    def get_hourly_forecast(self, lat, lon):
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}&hourly=temperature_2m,weathercode&forecast_days=1"
        )
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            data = r.json()
            temps = data["hourly"]["temperature_2m"][:12]
            codes = data["hourly"]["weathercode"][:12]
            times = data["hourly"]["time"][:12]
            # Simple weather code mapping
            code_map = {
                0: "Clear", 1: "Mainly Clear", 2: "Partly Cloudy", 3: "Overcast",
                45: "Fog", 48: "Depositing Rime Fog", 51: "Drizzle", 61: "Rain",
                71: "Snow", 80: "Rain Showers", 95: "Thunderstorm"
            }
            weather = [code_map.get(c, str(c)) for c in codes]
            return list(zip(times, temps, weather))
        except Exception as e:
            print(f"Error fetching forecast for {lat},{lon}: {e}")
            return []

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
        # Update scaled images on resize
        for panel in self.panels:
            panel.update_scaled_images()
        # Show current frame after resize
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