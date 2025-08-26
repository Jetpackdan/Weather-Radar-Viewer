import sys
import pathlib
import types
import pytest
import pytest, sys

# test_weather_radar_viewer.py
import importlib.machinery

# -------- Module loader (handles filename with hyphens) --------
@pytest.fixture(scope="session")
def module():
    here = pathlib.Path(__file__).parent
    target_path = here / "Weather-Radar-Viewer.py"
    if not target_path.exists():
        pytest.skip("Target module file not found")
    loader = importlib.machinery.SourceFileLoader("Weather_Radar_Viewer", str(target_path))
    mod = types.ModuleType(loader.name)
    loader.exec_module(mod)
    return mod

# -------- Patch missing Google helpers (force no API usage) --------
@pytest.fixture(autouse=True)
def patch_google(module, monkeypatch):
    # Ensure absent names won't raise
    monkeypatch.setattr(module, "google_city_suggestion", lambda raw: None, raising=False)
    monkeypatch.setattr(module, "google_geocode_lookup", lambda raw: None, raising=False)
    yield

# -------- Tests --------
def test_get_weather_icon_basic(module):
    f = module.get_weather_icon
    assert f("Heavy Rain") == "🌧"
    assert f("Light Snow") == "❄"
    assert f("Patchy Fog") == "🌫"
    assert f("Sunny") == "☀"

def test_get_weather_icon_fallback(module):
    icon = module.get_weather_icon("Volcanic Ash")  # not mapped
    assert icon == "🔆"

def test_world_px_monotonic(module):
    wp = module.world_px
    z = 4
    x1, y1 = wp(45.0, -120.0, z)
    x2, y2 = wp(45.0, -110.0, z)
    # Increasing longitude -> increasing x
    assert x2 > x1
    # Increasing latitude (toward pole) -> decreasing y in Web Mercator
    x3, y3 = wp(50.0, -120.0, z)
    assert y3 < y1

def test_suggest_city_name_fuzzy(module):
    # Misspelling (dropped 'a')
    sug = module.suggest_city_name("Portlnd, OR")
    assert sug == "Portland, OR"

def test_geocode_city_direct_coords(module, monkeypatch):
    # Avoid network during NOAA validation
    monkeypatch.setattr(module, "validate_noaa_point", lambda lat, lon: True)
    loc = module.geocode_city("45.00,-122.50")
    assert loc is not None
    assert abs(loc["lat"] - 45.0) < 1e-6
    assert abs(loc["lon"] - (-122.5)) < 1e-6
    assert "45.000" in loc["name"]

def test_validate_noaa_point_handles_failure(module, monkeypatch):
    class FakeResp:
        ok = False
        def json(self): return {}
    monkeypatch.setattr(module.requests, "get", lambda *a, **k: FakeResp())
    assert module.validate_noaa_point(0, 0) is False

def test_suggest_city_name_exact(module):
    assert module.suggest_city_name("Seattle, WA") == "Seattle, WA"

def test_geocode_city_returns_none_on_unknown(module, monkeypatch):
    # Force all network geocoding paths to fail
    def fake_get(*a, **k):
        class R:
            ok = False
            def json(self): return {}
        return R()
    monkeypatch.setattr(module.requests, "get", fake_get)
    monkeypatch.setattr(module, "validate_noaa_point", lambda lat, lon: False)
    # Input that is unlikely to fuzzy match (nonsense)
    assert module.geocode_city("Xyzabcville") is None

# Run via: pytest -q
if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))