
# Live Weather Radar Viewer using RainViewer API
import tkinter as tk
from PIL import Image, ImageTk
import requests
import io
import threading




# List of nearby NOAA radar stations (Oregon and vicinity)
STATIONS = {
	"Medford (KMAX)": "kmax",
	"Portland (KRTX)": "krtx",
	"Spokane (KOTX)": "kotx",
	"Eureka (KEWX)": "kbhx",
	"Boise (KCBX)": "cbyx"
}





class RadarViewer(tk.Tk):
	def __init__(self):
		super().__init__()
		self.title("NOAA Local Radar Viewer - Klamath Falls Area")
		self.geometry("650x600")

		self.station_var = tk.StringVar(value="Medford (KMAX)")
		self.dropdown = tk.OptionMenu(self, self.station_var, *STATIONS.keys(), command=self.on_station_change)
		self.dropdown.pack(pady=10)

		self.label = tk.Label(self)
		self.label.pack(expand=True, fill=tk.BOTH)
		self.refresh_interval = 300  # seconds
		self.after(0, self.update_radar)

	def get_radar_url(self):
		# RainViewer tile URL template
		TILE_URL_TEMPLATE = "https://tilecache.rainviewer.com/v2/radar/{time}/256/{z}/{lat}/{lon}/2/1_1.png"
		# Fetch latest radar frame time
		try:
			meta_resp = requests.get("https://api.rainviewer.com/public/weather-maps.json", timeout=10)
			meta_resp.raise_for_status()
			meta = meta_resp.json()
			frames = meta.get("radar", {}).get("nowcast", [])
			if not frames:
				print("No radar frames available from RainViewer.")
				return None
			latest_time = frames[-1]
		except Exception as e:
			print(f"Error fetching RainViewer metadata: {e}")
			return None

		# Klamath Falls, OR region: zoom=6, lat=25, lon=44 (approximate)
		z, lat, lon = 6, 25, 44
		return TILE_URL_TEMPLATE.format(time=latest_time, z=z, lat=lat, lon=lon)


	def fetch_radar_image(self):
		radar_url = self.get_radar_url()
		try:
			response = requests.get(radar_url, timeout=15)
			response.raise_for_status()
			content_type = response.headers.get('Content-Type', '')
			if 'image' not in content_type:
				print("Error: Response is not an image. Server returned:")
				print(response.text)
				return None
			image_data = response.content
			image = Image.open(io.BytesIO(image_data))
			image = image.resize((600, 550), Image.ANTIALIAS)
			return ImageTk.PhotoImage(image)
		except Exception as e:
			print(f"Error fetching radar image: {e}")
			return None

	def update_radar(self):
		def task():
			radar_img = self.fetch_radar_image()
			if radar_img:
				self.label.config(image=radar_img)
				self.label.image = radar_img
			self.after(self.refresh_interval * 1000, self.update_radar)
		threading.Thread(target=task, daemon=True).start()

	def on_station_change(self, *args):
		# Immediately update radar image when station changes
		radar_img = self.fetch_radar_image()
		if radar_img:
			self.label.config(image=radar_img)
			self.label.image = radar_img

if __name__ == "__main__":
	app = RadarViewer()
	app.mainloop()
	app.mainloop()
