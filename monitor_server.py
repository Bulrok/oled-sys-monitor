"""
Single-file Django server that exposes system metrics via LibreHardwareMonitorLib.dll
and serves a black, landscape-friendly webpage showing live stats.

Usage (PowerShell):
  - Place LibreHardwareMonitorLib.dll in the same directory as this script, or set env var LHM_DLL_PATH to its full path.
  - Install dependencies:  py -m pip install "Django>=4.2,<5.3" "pythonnet>=3.0,<4"
  - Run server:            py .\monitor_server.py --host 0.0.0.0 --port 8000

Notes:
  - Access the UI at http://<host>:<port>/
  - JSON metrics at http://<host>:<port>/api/metrics
  - Requires Windows with .NET Framework available (pythonnet) and LibreHardwareMonitorLib.dll.
"""

import argparse
import os
import sys
import ctypes
import time
from typing import Any, Dict, List, Optional, Tuple
import json
import configparser

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

if not is_admin():
    # Re-run the program with admin rights
    script = os.path.abspath(sys.argv[0])
    params = " ".join([f'"{arg}"' for arg in sys.argv[1:]])
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, f'"{script}" {params}', None, 1
    )
    sys.exit()

# ---- Your elevated code here ----
print("Running with admin privileges!")


# --- Hardware Monitor integration (LibreHardwareMonitor via pythonnet) ---
class HardwareMonitorReader:
    def __init__(self, dll_path: Optional[str] = None) -> None:
        self._load_monitor_lib(dll_path)
        # Lazily import after DLL is loaded
        # noqa: E402 (import after top-level)
        from LibreHardwareMonitor import Hardware as LHMHardware  # type: ignore
        Computer = LHMHardware.Computer
        self._lib = "LibreHardwareMonitor"

        self.computer = Computer()
        # Enable subsystems we need (Libre property names); try multiple aliases where applicable
        for prop in (
            "IsCpuEnabled",
            "IsGpuEnabled",
            "IsMemoryEnabled",
            "IsMotherboardEnabled",
            "IsMainboardEnabled",
            "IsControllerEnabled",
            "IsNetworkEnabled",
            "IsStorageEnabled",
        ):
            if hasattr(self.computer, prop):
                try:
                    setattr(self.computer, prop, True)
                except Exception:
                    pass

        self.computer.Open()

        # Use manual updates in _update_all; pythonnet proxying IVisitor can be unreliable

        # Enums and types cached for faster attribute access
        self.HardwareType = LHMHardware.HardwareType  # type: ignore
        self.SensorType = LHMHardware.SensorType  # type: ignore

        # Cache common enum members with cross-name compatibility
        self.HT_CPU = getattr(self.HardwareType, "Cpu", None) or getattr(self.HardwareType, "CPU", None)
        self.HT_MAINBOARD = getattr(self.HardwareType, "Mainboard", None) or getattr(self.HardwareType, "Motherboard", None)
        self.HT_RAM = getattr(self.HardwareType, "Memory", None) or getattr(self.HardwareType, "RAM", None)
        self.HT_NETWORK = getattr(self.HardwareType, "Network", None)
        self.ST_TEMP = getattr(self.SensorType, "Temperature", None)
        self.ST_LOAD = getattr(self.SensorType, "Load", None)
        self.ST_CLOCK = getattr(self.SensorType, "Clock", None)
        self.ST_POWER = getattr(self.SensorType, "Power", None)
        self.ST_DATA = getattr(self.SensorType, "Data", None)

    def _load_monitor_lib(self, dll_path: Optional[str]) -> None:
        # Load the LibreHardwareMonitor assembly following the simplest working approach
        # (mimicking the minimal example): import clr and AddReference to the DLL path.
        try:
            import clr  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "pythonnet (clr) is required. Install with: py -m pip install pythonnet"
            ) from exc

        candidate_paths: List[str] = []
        if dll_path:
            candidate_paths.append(dll_path)
        env_path = os.environ.get("LHM_DLL_PATH")
        if env_path:
            candidate_paths.append(env_path)

        # Current directory common names
        candidate_paths.append(os.path.join(os.getcwd(), "LibreHardwareMonitorLib.dll"))
        # Script directory
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            candidate_paths.append(os.path.join(script_dir, "LibreHardwareMonitorLib.dll"))
        except Exception:
            pass

        # Help Windows locate dependent DLLs by augmenting DLL search paths (Python 3.8+)
        try:
            add_dir = getattr(os, "add_dll_directory", None)
            if add_dir:
                seen_dirs: List[str] = []
                for p in list(candidate_paths):
                    directory = p if os.path.isdir(p) else os.path.dirname(p)
                    if directory and os.path.isdir(directory) and directory not in seen_dirs:
                        try:
                            add_dir(directory)
                            seen_dirs.append(directory)
                        except Exception:
                            pass
        except Exception:
            pass

        # Load LibreHardwareMonitorLib only (as in the working minimal example)
        loaded = False
        last_error: Optional[BaseException] = None
        # Try explicit file paths first
        for path in candidate_paths:
            try:
                if os.path.isdir(path):
                    # If a directory is provided, look for the DLL inside it
                    dll_full = os.path.join(path, "LibreHardwareMonitorLib.dll")
                else:
                    dll_full = path

                if os.path.exists(dll_full):
                    clr.AddReference(dll_full)  # type: ignore
                    loaded = True
                    break
            except Exception as exc:  # pragma: no cover - best-effort loading
                last_error = exc

        if not loaded:
            try:
                # Try by assembly name as a last resort
                clr.AddReference("LibreHardwareMonitorLib")  # type: ignore
                loaded = True
            except Exception as exc2:  # pragma: no cover
                last_error = exc2

        if not loaded:
            hint = (
                "Could not load LibreHardwareMonitorLib.dll. "
                "Place the DLL next to this script or set LHM_DLL_PATH to its full path."
            )
            raise FileNotFoundError(hint) from last_error

    def _update_all(self) -> None:
        # Ensure hardware and sub-hardware sensors are refreshed (manual traversal)
        for hardware in getattr(self.computer, "Hardware", []) or []:
            try:
                hardware.Update()
            except Exception:
                continue
            for sub in getattr(hardware, "SubHardware", []) or []:
                try:
                    sub.Update()
                except Exception:
                    continue

    def _collect_sensors(self) -> List[Tuple[Any, Any, Any]]:
        # Returns list of (hardware, sensor, parent_hardware)
        sensors: List[Tuple[Any, Any, Any]] = []
        for hardware in self.computer.Hardware:
            for sensor in getattr(hardware, "Sensors", []) or []:
                sensors.append((hardware, sensor, hardware))
            for sub in getattr(hardware, "SubHardware", []) or []:
                for sensor in getattr(sub, "Sensors", []) or []:
                    sensors.append((sub, sensor, hardware))
        return sensors

    @staticmethod
    def _is_finite(value: Optional[float]) -> bool:
        if value is None:
            return False
        try:
            import math
            v = float(value)
            return math.isfinite(v)
        except Exception:
            return False

    def _select_cpu_temps(self, sensors: List[Tuple[Any, Any, Any]]) -> Tuple[Optional[float], Optional[float]]:
        core_sensor_temps: List[float] = []
        ccd_sensor_temps: List[float] = []
        package_temps: List[float] = []
        mb_cpu_temps: List[float] = []
        for _hw, sensor, parent in sensors:
            if (self.ST_TEMP is not None and sensor.SensorType != self.ST_TEMP):
                continue
            name = (sensor.Name or "").lower()
            # CPU hardware temps
            if getattr(parent, "HardwareType", None) == self.HT_CPU:
                if "ccd" in name:
                    if self._is_finite(sensor.Value):
                        ccd_sensor_temps.append(float(sensor.Value))
                elif "core" in name:
                    if self._is_finite(sensor.Value):
                        core_sensor_temps.append(float(sensor.Value))
                elif any(k in name for k in ("package", "tdie", "tctl", "cpu")):
                    if self._is_finite(sensor.Value):
                        package_temps.append(float(sensor.Value))
            # Some boards expose CPU temp under mainboard/superIO
            elif getattr(parent, "HardwareType", None) == self.HT_MAINBOARD:
                if any(k in name for k in ("cpu", "cpu socket", "package", "tdie", "tctl")):
                    if self._is_finite(sensor.Value):
                        mb_cpu_temps.append(float(sensor.Value))

        # Hotspot: max core sensor, fallback to package, then mainboard
        hotspot: Optional[float] = None
        if core_sensor_temps:
            hotspot = max(core_sensor_temps)
        elif package_temps:
            hotspot = max(package_temps)
        elif mb_cpu_temps:
            hotspot = max(mb_cpu_temps)

        # Core temp: average of CCDs if present, else average of cores, else package/mainboard
        core_avg: Optional[float] = None
        vals: List[float] = []
        if ccd_sensor_temps:
            vals = ccd_sensor_temps
        elif core_sensor_temps:
            vals = core_sensor_temps
        elif package_temps:
            vals = package_temps
        elif mb_cpu_temps:
            vals = mb_cpu_temps
        if vals:
            core_avg = sum(vals) / len(vals)
        return core_avg, hotspot

    def _select_cpu_total_load(self, sensors: List[Tuple[Any, Any, Any]]) -> Optional[float]:
        for _hw, sensor, parent in sensors:
            if getattr(parent, "HardwareType", None) == self.HT_CPU and (self.ST_LOAD is None or sensor.SensorType == self.ST_LOAD):
                if (sensor.Name or "").strip().lower() in ("cpu total", "total"):  # common names
                    if self._is_finite(sensor.Value):
                        return float(sensor.Value)
        # Fallback: average all CPU load sensors
        loads: List[float] = []
        for _hw, sensor, parent in sensors:
            if getattr(parent, "HardwareType", None) == self.HT_CPU and (self.ST_LOAD is None or sensor.SensorType == self.ST_LOAD):
                if self._is_finite(sensor.Value):
                    loads.append(float(sensor.Value))
        if loads:
            return sum(loads) / len(loads)
        return None

    def _select_cpu_core_clocks(self, sensors: List[Tuple[Any, Any, Any]]) -> Tuple[Optional[float], Optional[float]]:
        clocks: List[float] = []
        for _hw, sensor, parent in sensors:
            if getattr(parent, "HardwareType", None) == self.HT_CPU and (self.ST_CLOCK is None or sensor.SensorType == self.ST_CLOCK):
                name = (sensor.Name or "").lower()
                if name.startswith("cpu core") or name.startswith("core") or "clock" in name:
                    if self._is_finite(sensor.Value):
                        clocks.append(float(sensor.Value))  # MHz
        if clocks:
            max_clock = max(clocks)
            avg_clock = sum(clocks) / len(clocks)
            return max_clock, avg_clock
        return None, None

    def _select_ram_used_free_gb(self, sensors: List[Tuple[Any, Any, Any]]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        used_gb: Optional[float] = None
        free_gb: Optional[float] = None
        load_percent: Optional[float] = None
        for _hw, sensor, parent in sensors:
            is_ram_hw = getattr(parent, "HardwareType", None) in (
                getattr(self.HardwareType, "RAM", None),
                getattr(self.HardwareType, "Memory", None),
            )
            if not is_ram_hw:
                continue
            name = (sensor.Name or "").lower()
            # Ignore Virtual Memory sensors; we only want physical Memory
            if "virtual" in name:
                continue
            # Prefer direct Data sensors for used/available in GB
            if (self.ST_DATA is not None and sensor.SensorType == self.ST_DATA) or (self.ST_DATA is None and sensor.SensorType.__str__().endswith("Data")):
                if "used" in name and self._is_finite(sensor.Value):
                    used_gb = float(sensor.Value)
                if ("available" in name or "free" in name) and self._is_finite(sensor.Value):
                    free_gb = float(sensor.Value)
            # Capture load percentage as a fallback
            if (self.ST_LOAD is None or sensor.SensorType == self.ST_LOAD) and (
                (name == "memory" or name == "ram") or ("memory" in name or "ram" in name) or ("load" in name or "usage" in name)
            ):
                if self._is_finite(sensor.Value):
                    load_percent = float(sensor.Value)
        return used_gb, free_gb, load_percent

    def _select_ram_usage(self, sensors: List[Tuple[Any, Any, Any]]) -> Optional[float]:
        # Prefer Load percentage if available
        for _hw, sensor, parent in sensors:
            is_ram_hw = getattr(parent, "HardwareType", None) in (
                getattr(self.HardwareType, "RAM", None),
                getattr(self.HardwareType, "Memory", None),
            )
            if is_ram_hw and (self.ST_LOAD is None or sensor.SensorType == self.ST_LOAD):
                if self._is_finite(sensor.Value):
                    return float(sensor.Value)
        # Fallback: derive from Used and Available
        used: Optional[float] = None
        available: Optional[float] = None
        for _hw, sensor, parent in sensors:
            is_ram_hw = getattr(parent, "HardwareType", None) in (
                getattr(self.HardwareType, "RAM", None),
                getattr(self.HardwareType, "Memory", None),
            )
            if not is_ram_hw:
                continue
            if (self.ST_DATA is not None and sensor.SensorType == self.ST_DATA) or (self.ST_DATA is None and sensor.SensorType.__str__().endswith("Data")):
                name = (sensor.Name or "").lower()
                if "used" in name:
                    if self._is_finite(sensor.Value):
                        used = float(sensor.Value)
                if "available" in name:
                    if self._is_finite(sensor.Value):
                        available = float(sensor.Value)
        if used is not None and available is not None and used + available > 0:
            return used / (used + available) * 100.0
        return None

    def _select_gpu_metrics(self, sensors: List[Tuple[Any, Any, Any]]) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
        temps: List[float] = []
        hotspot_temps: List[float] = []
        loads: List[float] = []
        powers: List[float] = []
        core_clocks: List[float] = []
        mem_clocks: List[float] = []
        gpu_types = [
            getattr(self.HardwareType, "GpuNvidia", None),
            getattr(self.HardwareType, "GpuAti", None),
            getattr(self.HardwareType, "GpuAmd", None),
            getattr(self.HardwareType, "GpuAmdRadeon", None),
            getattr(self.HardwareType, "GpuIntel", None),
        ]
        for _hw, sensor, parent in sensors:
            if getattr(parent, "HardwareType", None) in gpu_types:
                if self.ST_TEMP is None or sensor.SensorType == self.ST_TEMP:
                    name = (sensor.Name or "").lower()
                    if any(k in name for k in ("hot spot", "hotspot", "junction")):
                        if self._is_finite(sensor.Value):
                            hotspot_temps.append(float(sensor.Value))
                    else:
                        if self._is_finite(sensor.Value):
                            temps.append(float(sensor.Value))
                if self.ST_LOAD is None or sensor.SensorType == self.ST_LOAD:
                    name = (sensor.Name or "").lower()
                    if "core" in name or name in ("gpu core", "gpu"):
                        if self._is_finite(sensor.Value):
                            loads.append(float(sensor.Value))
                if (self.ST_POWER is not None and sensor.SensorType == self.ST_POWER) or (self.ST_POWER is None and sensor.SensorType.__str__().endswith("Power")):
                    # Some LHM builds expose SensorType.Power; string compare for safety
                    if self._is_finite(sensor.Value):
                        powers.append(float(sensor.Value))
                if self.ST_CLOCK is None or sensor.SensorType == self.ST_CLOCK:
                    name = (sensor.Name or "").lower()
                    if "memory" in name and self._is_finite(sensor.Value):
                        mem_clocks.append(float(sensor.Value))
                    elif ("gpu core" in name or "core" in name) and self._is_finite(sensor.Value):
                        core_clocks.append(float(sensor.Value))
        core_temp = max(temps) if temps else None
        hotspot = max(hotspot_temps) if hotspot_temps else None
        load = max(loads) if loads else None
        power = max(powers) if powers else None
        core_clock = max(core_clocks) if core_clocks else None
        mem_clock = max(mem_clocks) if mem_clocks else None
        return core_temp, hotspot, load, power, core_clock, mem_clock


    def read_metrics(self) -> Dict[str, Optional[float]]:
        self._update_all()
        sensors = self._collect_sensors()

        cpu_core_temp, cpu_hotspot_temp = self._select_cpu_temps(sensors)
        cpu_load = self._select_cpu_total_load(sensors)
        cpu_clock_max_mhz, cpu_clock_avg_mhz = self._select_cpu_core_clocks(sensors)
        ram_used_gb, ram_free_gb, ram_usage = self._select_ram_used_free_gb(sensors)
        gpu_core_temp, gpu_hotspot_temp, gpu_load, gpu_power, gpu_core_clock, gpu_mem_clock = self._select_gpu_metrics(sensors)
        # Network load (percent) across adapters; prefer a 'Total'/'Utilization' load, else max
        net_load_values: List[float] = []
        for _hw, sensor, parent in sensors:
            if getattr(parent, "HardwareType", None) == self.HT_NETWORK and (self.ST_LOAD is None or sensor.SensorType == self.ST_LOAD):
                name = (sensor.Name or "").strip().lower()
                if self._is_finite(sensor.Value):
                    # Heuristic: allow all load sensors, we'll pick preferred labels later
                    val = float(sensor.Value)
                    # Clip to sane range
                    if 0.0 <= val <= 100.0:
                        net_load_values.append(val)
        net_load: Optional[float] = None
        if net_load_values:
            # Prefer a representative high value as overall utilization
            net_load = max(net_load_values)

        # CPU package power (where available)
        cpu_powers: List[float] = []
        for _hw, sensor, parent in sensors:
            if getattr(parent, "HardwareType", None) == self.HT_CPU and (
                (self.ST_POWER is not None and sensor.SensorType == self.ST_POWER) or
                (self.ST_POWER is None and sensor.SensorType.__str__().endswith("Power"))
            ):
                name = (sensor.Name or "").lower()
                if any(k in name for k in ("package", "cpu", "ppt", "socket", "total")):
                    if self._is_finite(sensor.Value):
                        cpu_powers.append(float(sensor.Value))
        cpu_power = max(cpu_powers) if cpu_powers else None

        return {
            "cpu": {
                "core_temperature_c": _round_or_none(cpu_core_temp, 1),
                "hotspot_temperature_c": _round_or_none(cpu_hotspot_temp, 1),
                "usage_percent": _round_or_none(cpu_load, 1),
                "max_clock_mhz": _round_or_none(cpu_clock_max_mhz, 0),
                "avg_clock_mhz": _round_or_none(cpu_clock_avg_mhz, 0),
                "power_w": _round_or_none(cpu_power, 1),
            },
            "ram": {
                "usage_percent": _round_or_none(ram_usage, 1),
                "used_gb": _round_or_none(ram_used_gb, 1),
                "free_gb": _round_or_none(ram_free_gb, 1),
            },
            "gpu": {
                "core_temperature_c": _round_or_none(gpu_core_temp, 1),
                "hotspot_temperature_c": _round_or_none(gpu_hotspot_temp, 1),
                "usage_percent": _round_or_none(gpu_load, 1),
                "core_clock_mhz": _round_or_none(gpu_core_clock, 0),
                "memory_clock_mhz": _round_or_none(gpu_mem_clock, 0),
                "power_w": _round_or_none(gpu_power, 1),
            },
            "net": {
                "usage_percent": _round_or_none(net_load, 1),
            },
            "timestamp": time.time(),
        }


class CompositeMetricsReader:
    """Simple wrapper around OpenHardwareMonitor reader (no fallbacks)."""

    def __init__(self, dll_path: Optional[str]) -> None:
        self.hw = HardwareMonitorReader(dll_path=dll_path)

    def read_metrics(self) -> Dict[str, Optional[float]]:
        return self.hw.read_metrics()


 


def _round_or_none(value: Optional[float], digits: int) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except Exception:
        return None


# --- UI Config persistence (config.ini) ---
# Persist and serve UI options: sensor order and refresh interval.
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")

# Canonical sensor keys in initial/default order (must match HTML ids below)
SENSOR_KEYS_DEFAULT: List[str] = [
    "cpu_core_temp",
    "cpu_hotspot_temp",
    "cpu_usage",
    "net_load",  # new fourth position
    "cpu_clock_max",
    "cpu_clock_avg",
    "cpu_power",
    "ram_usage",
    "ram_used",
    "ram_free",
    "gpu_core_temp",
    "gpu_hotspot_temp",
    "gpu_clock",
    "gpu_mem_clock",
    "gpu_usage",
    "gpu_power",
]

UI_CONFIG: Dict[str, Any] = {
    "order": SENSOR_KEYS_DEFAULT.copy(),
    "update_interval_sec": 1.0,
}

ALIAS_TO_CANON: Dict[str, str] = {
    # Accept legacy key and map to current canonical key
    "cpu_temp": "cpu_core_temp",
}

CANON_TO_EXTERNAL: Dict[str, str] = {}


def _normalize_order(order: List[str]) -> List[str]:
    seen: set = set()
    normalized: List[str] = []
    for key in order:
        canon = ALIAS_TO_CANON.get(key, key)
        if canon in SENSOR_KEYS_DEFAULT and canon not in seen:
            normalized.append(canon)
            seen.add(canon)
    # Append any missing keys at the end, in default order
    for key in SENSOR_KEYS_DEFAULT:
        if key not in seen:
            normalized.append(key)
    return normalized


def load_ui_config() -> Dict[str, Any]:
    cfg = configparser.ConfigParser()
    result = {
        "order": SENSOR_KEYS_DEFAULT.copy(),
        "update_interval_sec": 1.0,
    }
    try:
        if os.path.exists(CONFIG_PATH):
            cfg.read(CONFIG_PATH)
            if cfg.has_section("ui"):
                order_str = cfg.get("ui", "order", fallback=",")
                order_list = [s.strip() for s in order_str.split(",") if s.strip()]
                result["order"] = _normalize_order(order_list)
                try:
                    result["update_interval_sec"] = max(0.05, float(cfg.get("ui", "update_interval_sec", fallback="1.0")))
                except Exception:
                    result["update_interval_sec"] = 1.0
    except Exception:
        # Ignore config read errors and use defaults
        pass
    return result


def save_ui_config(order: Optional[List[str]] = None, update_interval_sec: Optional[float] = None) -> None:
    cfg = configparser.ConfigParser()
    try:
        if os.path.exists(CONFIG_PATH):
            cfg.read(CONFIG_PATH)
    except Exception:
        cfg = configparser.ConfigParser()
    if not cfg.has_section("ui"):
        cfg.add_section("ui")
    if order is not None:
        canon_list = _normalize_order(order)
        external_list = [CANON_TO_EXTERNAL.get(k, k) for k in canon_list]
        cfg.set("ui", "order", ",".join(external_list))
    if update_interval_sec is not None:
        try:
            val = max(0.05, float(update_interval_sec))
        except Exception:
            val = 1.0
        cfg.set("ui", "update_interval_sec", f"{val:.3f}")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        cfg.write(f)

# --- Minimal Django setup ---
def configure_django() -> None:
    from django.conf import settings
    if settings.configured:
        return
    settings.configure(
        DEBUG=False,
        SECRET_KEY="monitor-server-secret-key",
        ROOT_URLCONF=__name__,
        ALLOWED_HOSTS=["*"],
        MIDDLEWARE=[],
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.staticfiles",
        ],
        TEMPLATES=[],
        STATIC_URL="/static/",
    )
    import django
    django.setup()


# Views
from django.http import HttpRequest, HttpResponse, JsonResponse  # type: ignore
from django.urls import path  # type: ignore


ohm_reader: Optional[CompositeMetricsReader] = None


def metrics_json(_request: HttpRequest) -> JsonResponse:
    if ohm_reader is None:
        return JsonResponse({"error": "LibreHardwareMonitor not initialized"}, status=500)
    data = ohm_reader.read_metrics()
    return JsonResponse(data)


def config_view(request: HttpRequest) -> JsonResponse:
    global UI_CONFIG
    if request.method == "GET":
        return JsonResponse({
            "order": UI_CONFIG.get("order", SENSOR_KEYS_DEFAULT),
            "update_interval_sec": UI_CONFIG.get("update_interval_sec", 1.0),
        })
    try:
        body = request.body.decode("utf-8") if request.body else "{}"
        payload = json.loads(body or "{}")
    except Exception:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    order = payload.get("order")
    update_interval_sec = payload.get("update_interval_sec")
    if order is not None and isinstance(order, list):
        UI_CONFIG["order"] = _normalize_order([str(x) for x in order])
    if update_interval_sec is not None:
        try:
            UI_CONFIG["update_interval_sec"] = max(0.05, float(update_interval_sec))
        except Exception:
            pass
    try:
        save_ui_config(order=UI_CONFIG.get("order"), update_interval_sec=UI_CONFIG.get("update_interval_sec"))
    except Exception:
        return JsonResponse({"error": "Failed to save config"}, status=500)
    return JsonResponse({"ok": True})


def index(_request: HttpRequest) -> HttpResponse:
    html = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="dark">
  <meta name="theme-color" content="#000000">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <title>System Monitor</title>
  <style>
    :root { --bg: #000; --fg: #fff; --accent: #9b9b9b; }
    html, body { margin:0; padding:0; height:100%; background:var(--bg); color:var(--fg); font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; }
    body { min-height: 100dvh; }
    .wrap { display:flex; flex-direction:column; justify-content:center; align-items:stretch; height:100%; padding: 12px 20px; box-sizing:border-box; padding: max(12px, env(safe-area-inset-top)) 20px max(12px, env(safe-area-inset-bottom)) calc(20px + 6px); }
    .grid { display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); grid-auto-rows: 1fr; gap: 10px 18px; }
    .item { display:flex; flex-direction:column; justify-content:center; align-items:flex-start; background:transparent; padding: 6px 0; border-radius:8px; }
    .value { font-size: 2.2rem; line-height:1.1; font-weight: 700; color: var(--fg); }
    .unit { opacity: 0.9; font-weight: 600; font-size: 1.1rem; margin-left: 6px; }
    .label-below { margin-top: 4px; font-size: 0.85rem; color: var(--accent); text-transform: uppercase; letter-spacing: .04em; }
    .fs-btn { position: fixed; right: 14px; top: 12px; z-index: 10; background: rgba(255,255,255,0.06); color:#fff; border: 1px solid rgba(255,255,255,0.15); border-radius: 10px; padding: 8px 12px; font-weight: 600; letter-spacing: .02em; }
    .settings-btn { position: fixed; right: 120px; top: 12px; z-index: 10; background: rgba(255,255,255,0.06); color:#fff; border: 1px solid rgba(255,255,255,0.15); border-radius: 10px; padding: 8px 12px; font-weight: 600; letter-spacing: .02em; }
    .reorder-btn { position: fixed; right: 220px; top: 12px; z-index: 10; background: rgba(255,255,255,0.06); color:#fff; border: 1px solid rgba(255,255,255,0.15); border-radius: 10px; padding: 8px 12px; font-weight: 600; letter-spacing: .02em; }
    .fs-btn:active { transform: scale(0.98); }
    .settings-btn:active { transform: scale(0.98); }
    .reorder-btn:active { transform: scale(0.98); }

    /* Settings overlay */
    .overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); backdrop-filter: blur(2px); display: none; align-items: center; justify-content: center; z-index: 20; }
    .overlay.open { display: flex; }
    .panel { background: #0f0f10; border: 1px solid rgba(255,255,255,0.15); border-radius: 12px; padding: 16px 18px; width: min(440px, 92vw); color: #fff; }
    .panel h2 { margin: 0 0 10px 0; font-size: 1.1rem; letter-spacing: .04em; }
    .field { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 12px 0; }
    .field label { font-size: .95rem; color: var(--accent); text-transform: uppercase; letter-spacing: .05em; }
    .field input { flex: 0 0 140px; background: #161618; color:#fff; border: 1px solid rgba(255,255,255,0.18); border-radius: 8px; padding: 8px 10px; font-size: 1rem; }
    .dialog-actions { display:flex; justify-content: flex-end; gap: 10px; margin-top: 10px; }
    .btn { background: rgba(255,255,255,0.08); color:#fff; border: 1px solid rgba(255,255,255,0.18); border-radius: 10px; padding: 8px 12px; font-weight: 600; letter-spacing: .02em; }
    /* Reorder mode styles */
    body.reorder .item { cursor: grab; background: rgba(255,255,255,0.02); border: 1px dashed rgba(255,255,255,0.18); padding: 6px 8px; border-radius: 8px; }
    .item.placeholder { background: rgba(255,255,255,0.06); border: 1px dashed rgba(255,255,255,0.3); border-radius: 8px; }
    .dragging { opacity: 0.95; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }
    @media (orientation: landscape) {
      .grid { grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px 22px; }
      .value { font-size: 2.3rem; }
    }
  </style>
  <script>
    const COLOR_MAP = 'rdyblu'; // 'magma' | 'inferno' | 'viridis' | 'lava' | 'rdyblu'
    const COLOR_STOPS = {
      // Expanded stops and slightly lighter endpoints for visibility on black
      magma: [
        [0.000, [0,   0,   4]],
        [0.125, [28,  16,  68]],
        [0.250, [79,  18,  123]],
        [0.375, [129, 37,  129]],
        [0.500, [178, 54,  121]],
        [0.625, [214, 87,  105]],
        [0.750, [236, 135, 109]],
        [0.875, [248, 185, 121]],
        [1.000, [252, 232, 164]],
      ],
      inferno: [
        [0.000, [0,   0,   4]],
        [0.125, [31,  12,  72]],
        [0.250, [85,  15,  109]],
        [0.375, [136, 34,  106]],
        [0.500, [186, 54,  85]],
        [0.625, [227, 89,  51]],
        [0.750, [249, 140, 10]],
        [0.875, [252, 194, 65]],
        [1.000, [255, 240, 170]],
      ],
      viridis: [
        [0.000, [68,  1,   84]],
        [0.125, [72,  33,  115]],
        [0.250, [64,  67,  135]],
        [0.375, [52,  94,  141]],
        [0.500, [41,  120, 142]],
        [0.625, [32,  144, 140]],
        [0.750, [34,  167, 132]],
        [0.875, [94,  201, 98]],
        [1.000, [253, 231, 37]],
      ],
      // Red-Yellow-Blue diverging (low=blue, mid=yellow, high=red)
      rdyblu: [
        [0.000, [49, 54, 149]],
        [0.111, [69, 117, 180]],
        [0.222, [116, 173, 209]],
        [0.333, [171, 217, 233]],
        [0.444, [224, 243, 248]],
        [0.555, [254, 224, 144]],
        [0.666, [253, 174, 97]],
        [0.777, [244, 109, 67]],
        [0.888, [215, 48, 39]],
        [1.000, [165, 0, 38]],
      ],
      lava: [
        [0.000, [10,  0,   0]],
        [0.125, [60,  0,   10]],
        [0.250, [110, 10,  20]],
        [0.375, [160, 25,  20]],
        [0.500, [200, 40,  10]],
        [0.625, [230, 70,  10]],
        [0.750, [245, 110, 20]],
        [0.875, [255, 160, 40]],
        [1.000, [255, 220, 120]],
      ],
    };
    function clamp01(x){ return Math.max(0, Math.min(1, x)); }
    function lerp(a,b,t){ return a + (b - a) * t; }
    function lerpColor(c1, c2, t){
      return [
        Math.round(lerp(c1[0], c2[0], t)),
        Math.round(lerp(c1[1], c2[1], t)),
        Math.round(lerp(c1[2], c2[2], t))
      ];
    }
    const LIGHTEN = 0.14; // mix with white for better readability on black
    function lightenColor(rgb, amt){
      const r = Math.round(rgb[0] + (255 - rgb[0]) * amt);
      const g = Math.round(rgb[1] + (255 - rgb[1]) * amt);
      const b = Math.round(rgb[2] + (255 - rgb[2]) * amt);
      return [r,g,b];
    }
    function sampleColorMap(t){
      const stops = COLOR_STOPS[COLOR_MAP] || COLOR_STOPS.magma;
      t = clamp01(t);
      for (let i = 0; i < stops.length - 1; i++){
        const [p1, c1] = stops[i];
        const [p2, c2] = stops[i+1];
        if (t >= p1 && t <= p2){
          const nt = (t - p1) / (p2 - p1);
          const rgb = lerpColor(c1, c2, nt);
          const [r,g,b] = lightenColor(rgb, LIGHTEN);
          return `rgb(${r}, ${g}, ${b})`;
        }
      }
      const last = lightenColor(stops[stops.length - 1][1], LIGHTEN);
      return `rgb(${last[0]}, ${last[1]}, ${last[2]})`;
    }
    function valueToColor(value, unit){
      if (value === null || value === undefined) return 'var(--fg)';
      let percent = 0;
      if (unit === '%') {
        percent = value;
      } else if (unit.indexOf('°C') !== -1) {
        percent = value; // assume 0-100°C range
      } else {
        return 'var(--fg)';
      }
      return sampleColorMap(clamp01(percent / 100));
    }
    function updateViewportHeightVar() {
      const vh = Math.max(document.documentElement.clientHeight, window.innerHeight || 0);
      document.documentElement.style.setProperty('--vhpx', vh + 'px');
    }
    function applyViewportFix() {
      updateViewportHeightVar();
      window.addEventListener('resize', updateViewportHeightVar);
      window.addEventListener('orientationchange', () => setTimeout(updateViewportHeightVar, 300));
      document.addEventListener('visibilitychange', () => setTimeout(updateViewportHeightVar, 300));
    }
    async function enterFullscreen() {
      try {
        const el = document.documentElement;
        if (!document.fullscreenElement && el.requestFullscreen) {
          await el.requestFullscreen({ navigationUI: 'hide' }).catch(()=>{});
        }
        if (!document.fullscreenElement && el.webkitRequestFullscreen) {
          el.webkitRequestFullscreen();
        }
        if (screen.orientation && screen.orientation.lock) {
          try { await screen.orientation.lock('landscape'); } catch (e) { /* ignore */ }
        }
      } catch (e) { console.warn('Fullscreen failed', e); }
    }
    async function onFSButton() { await enterFullscreen(); updateFSButtonVisibility(); }
    function onFirstInteract() {
      enterFullscreen();
      document.body.removeEventListener('click', onFirstInteract);
      document.body.removeEventListener('touchstart', onFirstInteract);
    }
    function inStandaloneDisplay(){
      return (window.matchMedia && (window.matchMedia('(display-mode: standalone)').matches || window.matchMedia('(display-mode: fullscreen)').matches)) || (window.navigator && window.navigator.standalone === true);
    }
    function updateFSButtonVisibility(){
      const fsBtn = document.querySelector('.fs-btn');
      const stBtn = document.querySelector('.settings-btn');
      const reBtn = document.querySelector('.reorder-btn');
      const shouldHide = !!document.fullscreenElement || inStandaloneDisplay();
      if (fsBtn) fsBtn.style.display = shouldHide ? 'none' : 'inline-block';
      if (stBtn) stBtn.style.display = shouldHide ? 'none' : 'inline-block';
      if (reBtn) reBtn.style.display = shouldHide ? 'none' : 'inline-block';
    }
    // --- Keep screen awake while in fullscreen ---
    let wakeLock = null;
    async function requestWakeLock() {
      try {
        if ('wakeLock' in navigator && !wakeLock && !document.hidden) {
          wakeLock = await navigator.wakeLock.request('screen');
          wakeLock.addEventListener('release', () => { wakeLock = null; });
        }
      } catch (e) { console.warn('WakeLock request failed', e); wakeLock = null; }
    }
    async function releaseWakeLock() {
      try { if (wakeLock) { await wakeLock.release(); } } catch (_) {} finally { wakeLock = null; }
    }
    function syncWakeLockWithFullscreen() {
      if (document.fullscreenElement && !document.hidden) { requestWakeLock(); }
      else { releaseWakeLock(); }
    }

    let UPDATE_MS = 1000; // default update interval (ms)
    let pollTimer = null;
    let IS_REORDER = false;

    async function fetchMetrics() {
      try {
        const res = await fetch('/api/metrics', { cache: 'no-store' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const m = await res.json();
        setValue('cpu_core_temp', m.cpu?.core_temperature_c, '°C');
        setValue('cpu_hotspot_temp', m.cpu?.hotspot_temperature_c, '°C');
        setValue('cpu_usage', m.cpu?.usage_percent, '%');
        setValue('cpu_clock_max', m.cpu?.max_clock_mhz, 'MHz');
        setValue('net_load', m.net?.usage_percent, '%');
        setValue('cpu_clock_avg', m.cpu?.avg_clock_mhz, 'MHz');
        setValue('cpu_power', m.cpu?.power_w, 'W');
        setValue('ram_usage', m.ram?.usage_percent, '%');
        setValue('ram_used', m.ram?.used_gb, 'GB');
        setValue('ram_free', m.ram?.free_gb, 'GB');
        setValue('gpu_core_temp', m.gpu?.core_temperature_c, '°C');
        setValue('gpu_hotspot_temp', m.gpu?.hotspot_temperature_c, '°C');
        setValue('gpu_clock', m.gpu?.core_clock_mhz, 'MHz');
        setValue('gpu_mem_clock', m.gpu?.memory_clock_mhz, 'MHz');
        setValue('gpu_usage', m.gpu?.usage_percent, '%');
        setValue('gpu_power', m.gpu?.power_w, 'W');
      } catch (e) {
        console.error(e);
      }
    }

    function startPolling(){
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(fetchMetrics, UPDATE_MS);
    }

    function openSettings(){
      const ov = document.getElementById('settings-overlay');
      if (ov) ov.classList.add('open');
      const input = document.getElementById('update-interval');
      if (input) input.value = (UPDATE_MS / 1000).toString();
    }

    function closeSettings(){
      const ov = document.getElementById('settings-overlay');
      if (ov) ov.classList.remove('open');
    }

    function onUpdateIntervalChange(){
      const input = document.getElementById('update-interval');
      if (!input) return;
      const secs = parseFloat(input.value);
      if (!isNaN(secs) && secs > 0.05) {
        UPDATE_MS = Math.round(secs * 1000);
        startPolling();
        // Persist updated interval
        saveConfig({ update_interval_sec: secs });
      }
    }
    function setValue(id, value, unit) {
      const el = document.getElementById(id);
      if (!el) return;
      const v = (value === null || value === undefined) ? '—' : value.toString();
      const vEl = el.querySelector('.v');
      vEl.textContent = v;
      // Color only the numeric part using the selected colormap for °C and %
      vEl.style.color = valueToColor(value, unit);
      el.querySelector('.unit').textContent = unit || '';
    }
    // --- Reorder & Config persistence ---
    function getCurrentOrder(){
      return Array.from(document.querySelectorAll('.grid .item')).map(el => el.dataset.key);
    }
    async function loadConfig(){
      try {
        const res = await fetch('/api/config', { cache: 'no-store' });
        if (res.ok) {
          const c = await res.json();
          if (Array.isArray(c.order)) applyOrder(c.order);
          if (typeof c.update_interval_sec === 'number' && c.update_interval_sec > 0.05) {
            UPDATE_MS = Math.round(c.update_interval_sec * 1000);
            const input = document.getElementById('update-interval');
            if (input) input.value = (c.update_interval_sec).toString();
          }
        }
      } catch {}
    }
    function applyOrder(order){
      const grid = document.querySelector('.grid');
      if (!grid || !order) return;
      const map = new Map(Array.from(grid.children).map(el => [el.dataset.key, el]));
      order.forEach(key => {
        const el = map.get(key);
        if (el) grid.appendChild(el);
      });
    }
    async function saveConfig(partial){
      const payload = Object.assign({
        order: getCurrentOrder(),
        update_interval_sec: UPDATE_MS / 1000,
      }, partial || {});
      try {
        await fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
      } catch {}
    }
    function toggleReorder(){
      IS_REORDER = !IS_REORDER;
      document.body.classList.toggle('reorder', IS_REORDER);
    }
    function setupDrag(){
      const grid = document.querySelector('.grid');
      if (!grid) return;
      grid.querySelectorAll('.item').forEach(item => {
        item.addEventListener('pointerdown', onPointerDown);
      });
    }
    let dragEl = null; let placeholder = null; let offsetX = 0; let offsetY = 0;
    function onPointerDown(e){
      if (!IS_REORDER) return;
      const item = e.currentTarget;
      dragEl = item;
      const rect = item.getBoundingClientRect();
      offsetX = e.clientX - rect.left; offsetY = e.clientY - rect.top;
      placeholder = document.createElement('div');
      placeholder.className = 'item placeholder';
      placeholder.style.height = rect.height + 'px';
      item.parentNode.insertBefore(placeholder, item.nextSibling);
      item.classList.add('dragging');
      item.style.position = 'fixed';
      item.style.left = rect.left + 'px';
      item.style.top = rect.top + 'px';
      item.style.width = rect.width + 'px';
      item.style.pointerEvents = 'none';
      document.addEventListener('pointermove', onPointerMove);
      document.addEventListener('pointerup', onPointerUp, { once: true });
      e.preventDefault();
    }
    function onPointerMove(e){
      if (!dragEl) return;
      moveTo(e.clientX, e.clientY);
    }
    function moveTo(x, y){
      if (!dragEl) return;
      dragEl.style.left = (x - offsetX) + 'px';
      dragEl.style.top = (y - offsetY) + 'px';
      const grid = document.querySelector('.grid');
      const items = Array.from(grid.querySelectorAll('.item')).filter(el => el !== dragEl && el !== placeholder);
      let nearest = null; let nearestDist = Infinity;
      items.forEach(el => {
        const r = el.getBoundingClientRect();
        const cx = r.left + r.width / 2; const cy = r.top + r.height / 2;
        const dx = cx - x; const dy = cy - y; const d = dx*dx + dy*dy;
        if (d < nearestDist){ nearestDist = d; nearest = el; }
      });
      if (nearest){
        const r = nearest.getBoundingClientRect();
        const before = y < r.top + r.height / 2;
        nearest.parentNode.insertBefore(placeholder, before ? nearest : nearest.nextSibling);
      }
    }
    function onPointerUp(){
      document.removeEventListener('pointermove', onPointerMove);
      if (!dragEl || !placeholder) { dragEl = null; placeholder = null; return; }
      placeholder.parentNode.insertBefore(dragEl, placeholder);
      dragEl.classList.remove('dragging');
      dragEl.style.position = ''; dragEl.style.left = ''; dragEl.style.top = ''; dragEl.style.width = ''; dragEl.style.pointerEvents = '';
      placeholder.parentNode.removeChild(placeholder);
      dragEl = null; placeholder = null;
      saveConfig({ order: getCurrentOrder() });
    }

    window.addEventListener('load', async () => {
      await loadConfig();
      fetchMetrics();
      startPolling();
      setupDrag();
      document.body.addEventListener('click', onFirstInteract, { once: true });
      document.body.addEventListener('touchstart', onFirstInteract, { once: true });
      document.addEventListener('fullscreenchange', updateViewportHeightVar);
      document.addEventListener('fullscreenchange', updateFSButtonVisibility);
      // wake lock lifecycle
      document.addEventListener('fullscreenchange', syncWakeLockWithFullscreen);
      document.addEventListener('visibilitychange', syncWakeLockWithFullscreen);
      window.addEventListener('pageshow', syncWakeLockWithFullscreen);
      window.addEventListener('pagehide', releaseWakeLock);
      applyViewportFix();
      updateFSButtonVisibility();
      document.addEventListener('visibilitychange', updateFSButtonVisibility);
      // ensure correct initial state
      syncWakeLockWithFullscreen();
      // overlay close on backdrop click
      const ov = document.getElementById('settings-overlay');
      if (ov) ov.addEventListener('click', (e)=>{ if (e.target === ov) closeSettings(); });
    });
  </script>
  <link rel="manifest" href="data:application/manifest+json,{\"name\":\"System Monitor\",\"short_name\":\"Monitor\",\"display\":\"fullscreen\",\"background_color\":\"#000000\",\"theme_color\":\"#000000\"}">
</head>
<body>
  <div class="wrap">
    <button class="fs-btn" onclick="onFSButton()">Fullscreen</button>
    <button class="reorder-btn" onclick="toggleReorder()">Reorder</button>
    <button class="settings-btn" onclick="openSettings()">Settings</button>
    <div id="settings-overlay" class="overlay">
      <div class="panel">
        <h2>Settings</h2>
        <div class="field">
          <label for="update-interval">Update Interval (seconds)</label>
          <input id="update-interval" type="number" step="0.1" min="0.1" value="1" oninput="onUpdateIntervalChange()" />
        </div>
        <div class="dialog-actions">
          <button class="btn" onclick="closeSettings()">Close</button>
        </div>
      </div>
    </div>
    <div class="grid">
      <div class="item" data-key="cpu_core_temp"><div class="value" id="cpu_core_temp"><span class="v">—</span><span class="unit">°C</span></div><div class="label-below">CPU Core Temp</div></div>
      <div class="item" data-key="cpu_hotspot_temp"><div class="value" id="cpu_hotspot_temp"><span class="v">—</span><span class="unit">°C</span></div><div class="label-below">CPU Hot Spot</div></div>
      <div class="item" data-key="cpu_usage"><div class="value" id="cpu_usage"><span class="v">—</span><span class="unit">%</span></div><div class="label-below">CPU Usage</div></div>
      <div class="item" data-key="cpu_clock_max"><div class="value" id="cpu_clock_max"><span class="v">—</span><span class="unit">MHz</span></div><div class="label-below">CPU Max Clock</div></div>
      <div class="item" data-key="net_load"><div class="value" id="net_load"><span class="v">—</span><span class="unit">%</span></div><div class="label-below">Network Load</div></div>
      <div class="item" data-key="cpu_clock_avg"><div class="value" id="cpu_clock_avg"><span class="v">—</span><span class="unit">MHz</span></div><div class="label-below">CPU Avg Clock</div></div>
      <div class="item" data-key="cpu_power"><div class="value" id="cpu_power"><span class="v">—</span><span class="unit">W</span></div><div class="label-below">CPU Power</div></div>

      <div class="item" data-key="ram_usage"><div class="value" id="ram_usage"><span class="v">—</span><span class="unit">%</span></div><div class="label-below">RAM Usage</div></div>
      <div class="item" data-key="ram_used"><div class="value" id="ram_used"><span class="v">—</span><span class="unit">GB</span></div><div class="label-below">RAM Used</div></div>
      <div class="item" data-key="ram_free"><div class="value" id="ram_free"><span class="v">—</span><span class="unit">GB</span></div><div class="label-below">RAM Free</div></div>

      <div class="item" data-key="gpu_core_temp"><div class="value" id="gpu_core_temp"><span class="v">—</span><span class="unit">°C</span></div><div class="label-below">GPU Core Temp</div></div>
      <div class="item" data-key="gpu_hotspot_temp"><div class="value" id="gpu_hotspot_temp"><span class="v">—</span><span class="unit">°C</span></div><div class="label-below">GPU Hot Spot</div></div>
      <div class="item" data-key="gpu_clock"><div class="value" id="gpu_clock"><span class="v">—</span><span class="unit">MHz</span></div><div class="label-below">GPU Clock</div></div>
      <div class="item" data-key="gpu_mem_clock"><div class="value" id="gpu_mem_clock"><span class="v">—</span><span class="unit">MHz</span></div><div class="label-below">GPU Mem Clock</div></div>
      <div class="item" data-key="gpu_usage"><div class="value" id="gpu_usage"><span class="v">—</span><span class="unit">%</span></div><div class="label-below">GPU Usage</div></div>
      <div class="item" data-key="gpu_power"><div class="value" id="gpu_power"><span class="v">—</span><span class="unit">W</span></div><div class="label-below">GPU Power</div></div>
    </div>
  </div>
</body>
</html>
    """
    return HttpResponse(html)


urlpatterns = [
    path("", index),
    path("api/metrics", metrics_json),
    path("api/config", config_view),
]


def main() -> None:
    global ohm_reader
    global UI_CONFIG
    parser = argparse.ArgumentParser(description="LibreHardwareMonitor Django server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--port", default="8000", help="Port to bind (default: 8000)")
    parser.add_argument("--lhm-dll", dest="lhm_dll", default=None, help="Path to LibreHardwareMonitorLib.dll (optional)")
    args = parser.parse_args()

    # Load UI config (order and refresh interval) at startup
    try:
        UI_CONFIG = load_ui_config()
    except Exception:
        UI_CONFIG = {"order": SENSOR_KEYS_DEFAULT.copy(), "update_interval_sec": 1.0}

    ohm_reader = CompositeMetricsReader(dll_path=args.lhm_dll)

    configure_django()

    from django.core.management import call_command  # type: ignore
    call_command("runserver", f"{args.host}:{args.port}", use_reloader=False, verbosity=1)


if __name__ == "__main__":
    main()


