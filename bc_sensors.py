"""
Live hardware sensors.

Windows:   LibreHardwareMonitorLib (bundled in ./lhm, loaded through pythonnet) gives CPU, GPU,
           motherboard, fan, drive and memory sensors. CPU and motherboard sensors also need
           the PawnIO driver. If the library can't load, a fallback reads load from psutil,
           NVIDIA GPUs from nvidia-smi, and the ACPI thermal zone.
Elsewhere: psutil load / memory only (or synthetic data when BUILDCHECK_DEMO is set).

Every backend's read() returns a snapshot:
    {"groups": [{"hw": "Cpu", "name": "...", "sensors": [{"type", "name", "value", "unit"}]}],
     "summary": {"cpu_temp", "cpu_load", "cpu_power", "cpu_clock", "tjmax_dist", "gpu_name",
                 "gpu_temp", "gpu_hotspot", "gpu_load", "gpu_power", "gpu_clock", "ram_pct",
                 "fans": [(name, rpm)]}}
"""

import math
import os
import random
import shutil
import sys
import time

from bc_common import DEMO, IS_WIN, lhm_dir, log, run

UNITS = {"Temperature": "°C", "Load": "%", "Clock": "MHz", "Power": "W", "Voltage": "V", "Fan": "RPM",
         "Control": "%", "Data": "GB", "SmallData": "MB", "Throughput": "B/s", "Level": "%", "Frequency": "Hz",
         "Current": "A", "Energy": "mWh", "Flow": "L/h", "Factor": "", "TimeSpan": "s"}
TYPE_ORDER = {"Temperature": 0, "Load": 1, "Clock": 2, "Power": 3, "Fan": 4, "Control": 5, "Voltage": 6,
              "Current": 7, "Data": 8, "SmallData": 9, "Throughput": 10}
HW_LABEL = {"Cpu": "CPU", "GpuNvidia": "GPU", "GpuAmd": "GPU", "GpuIntel": "GPU", "Memory": "Memory",
            "Motherboard": "Motherboard", "SuperIO": "Motherboard sensors", "Storage": "Drive", "Cooler": "Cooler",
            "EmbeddedController": "Embedded controller", "Psu": "Power supply", "Network": "Network",
            "Battery": "Battery"}


def pawnio_installed():
    if not IS_WIN:
        return False
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\PawnIO"))
        return True
    except OSError:
        return False


def lhm_available():
    return IS_WIN and os.path.isfile(os.path.join(lhm_dir(), "LibreHardwareMonitorLib.dll"))


def _first(sensors, typ, *names, contains=False):
    for want in names:
        for s in sensors:
            if s["type"] == typ and s["value"] is not None:
                if (want.lower() in s["name"].lower()) if contains else (s["name"] == want):
                    return s["value"]
    return None


def summarize(groups):
    """Pull the headline numbers out of a list of sensor groups."""
    sm = {"fans": []}
    cpu = [g for g in groups if g["hw"] == "Cpu"]
    if cpu:
        ss = cpu[0]["sensors"]
        sm["cpu_temp"] = _first(ss, "Temperature", "CPU Package", "Core (Tctl/Tdie)", "Core (Tdie)", "Core (Tctl)",
                                "Core Max", "Core Average", "Package")
        if sm["cpu_temp"] is None:
            temps = [s["value"] for s in ss if s["type"] == "Temperature" and s["value"] and "distance" not in s["name"].lower()]
            sm["cpu_temp"] = max(temps) if temps else None
        sm["cpu_load"] = _first(ss, "Load", "CPU Total")
        sm["cpu_power"] = _first(ss, "Power", "CPU Package", "Package")
        clocks = [s["value"] for s in ss if s["type"] == "Clock" and s["value"] and s["name"].startswith("Core #")]
        sm["cpu_clock"] = sum(clocks) / len(clocks) if clocks else _first(ss, "Clock", "Cores (Average Effective)",
                                                                         "Cores (Average)", contains=True)
        dist = [s["value"] for s in ss if s["type"] == "Temperature" and s["value"] is not None
                and "distance to tjmax" in s["name"].lower()]
        sm["tjmax_dist"] = min(dist) if dist else None
        sm["cpu_name"] = cpu[0]["name"]
    gpus = [g for g in groups if g["hw"].startswith("Gpu")]
    gpus.sort(key=lambda g: 1 if g["hw"] == "GpuIntel" else 0)
    if gpus:
        ss = gpus[0]["sensors"]
        sm["gpu_name"] = gpus[0]["name"]
        sm["gpu_temp"] = _first(ss, "Temperature", "GPU Core", "GPU Temperature", "Core")
        sm["gpu_hotspot"] = _first(ss, "Temperature", "GPU Hot Spot", "Hot Spot")
        sm["gpu_mem_temp"] = _first(ss, "Temperature", "GPU Memory Junction", "GPU Memory")
        sm["gpu_load"] = _first(ss, "Load", "GPU Core", "D3D 3D")
        sm["gpu_power"] = _first(ss, "Power", "GPU Package", "GPU Power", "GPU Board Power", "Package", contains=True)
        sm["gpu_clock"] = _first(ss, "Clock", "GPU Core", "Core")
    mem = [g for g in groups if g["hw"] == "Memory"]
    if mem:
        sm["ram_pct"] = _first(mem[0]["sensors"], "Load", "Memory")
    for g in groups:
        for s in g["sensors"]:
            if s["type"] == "Fan" and s["value"] is not None:
                prefix = HW_LABEL.get(g["hw"], g["hw"])
                name = s["name"] if g["hw"] in ("SuperIO", "Motherboard") or s["name"].startswith(prefix) \
                    else f"{prefix} {s['name']}"
                sm["fans"].append((name, s["value"]))
    return sm


# ------------------------------------------------------------------ LibreHardwareMonitor
class LHMBackend:
    name = "LibreHardwareMonitor"

    def __init__(self):
        folder = lhm_dir()
        if folder not in sys.path:
            sys.path.append(folder)
        os.environ.setdefault("PYTHONNET_RUNTIME", "netfx")
        import pythonnet
        try:
            pythonnet.load("netfx")
        except Exception:                            # noqa: BLE001 - already loaded
            pass
        import clr                                  # noqa: F401
        clr.AddReference(os.path.join(folder, "LibreHardwareMonitorLib.dll"))
        from LibreHardwareMonitor.Hardware import Computer   # noqa: E402
        c = Computer()
        c.IsCpuEnabled = True
        c.IsGpuEnabled = True
        c.IsMemoryEnabled = True
        c.IsMotherboardEnabled = True
        c.IsControllerEnabled = True
        c.IsStorageEnabled = True
        try:
            c.IsPsuEnabled = True
        except Exception:                            # noqa: BLE001 - older library
            pass
        c.Open()
        self.c = c
        self.note = "" if pawnio_installed() else (
            "CPU and motherboard temperatures need the PawnIO driver, which isn't installed.")

    def _sensors(self, hw):
        out = []
        for s in hw.Sensors:
            typ = str(s.SensorType)
            v = s.Value
            out.append({"type": typ, "name": str(s.Name), "value": None if v is None else float(v),
                        "unit": UNITS.get(typ, "")})
        return out

    def read(self):
        groups = []
        for hw in self.c.Hardware:
            try:
                hw.Update()
            except Exception:                        # noqa: BLE001
                continue
            hwt = str(hw.HardwareType)
            groups.append({"hw": hwt, "name": str(hw.Name), "sensors": self._sensors(hw)})
            for sub in hw.SubHardware:
                try:
                    sub.Update()
                except Exception:                    # noqa: BLE001
                    continue
                groups.append({"hw": str(sub.HardwareType), "name": str(sub.Name), "sensors": self._sensors(sub)})
        for g in groups:
            g["sensors"].sort(key=lambda s: (TYPE_ORDER.get(s["type"], 99), s["name"]))
        groups = [g for g in groups if g["sensors"]]
        return {"groups": groups, "summary": summarize(groups)}

    def close(self):
        try:
            self.c.Close()
        except Exception:                            # noqa: BLE001
            pass


# ------------------------------------------------------------------ fallback
class BasicBackend:
    name = "basic"

    def __init__(self):
        try:
            import psutil
            psutil.cpu_percent(percpu=True)
            self.ps = psutil
        except ImportError:
            self.ps = None
        self.smi = None
        if IS_WIN or sys.platform.startswith("linux"):
            self.smi = shutil.which("nvidia-smi") or (r"C:\Windows\System32\nvidia-smi.exe"
                                                      if IS_WIN and os.path.exists(r"C:\Windows\System32\nvidia-smi.exe") else None)
        self.acpi = None
        self.acpi_t = 0
        if IS_WIN:
            self.note = ("Limited sensors: the bundled LibreHardwareMonitor library didn't load, so CPU temperature "
                         "comes from the ACPI thermal zone (approximate) and fans aren't shown.")
        else:
            self.note = "Temperature and fan sensors are only read on Windows."

    def _acpi_temp(self):
        if not IS_WIN:
            return None
        if time.time() - self.acpi_t > 5:
            self.acpi_t = time.time()
            rc, out, _ = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              "Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature | "
                              "ForEach-Object { $_.CurrentTemperature }"], timeout=8)
            vals = []
            for line in out.split():
                try:
                    vals.append(int(line) / 10 - 273.15)
                except ValueError:
                    pass
            vals = [v for v in vals if 0 < v < 120]
            self.acpi = max(vals) if vals else None
        return self.acpi

    def _nvidia(self):
        if not self.smi:
            return None
        rc, out, _ = run([self.smi, "--query-gpu=name,temperature.gpu,utilization.gpu,power.draw,clocks.gr,fan.speed,"
                          "memory.used,memory.total", "--format=csv,noheader,nounits"], timeout=5)
        if rc != 0 or not out.strip():
            return None
        parts = [p.strip() for p in out.strip().splitlines()[0].split(",")]

        def num(x):
            try:
                return float(x)
            except ValueError:
                return None
        name = parts[0]
        vals = [num(p) for p in parts[1:]]
        sensors = [("Temperature", "GPU Core", vals[0]), ("Load", "GPU Core", vals[1]), ("Power", "GPU Package", vals[2]),
                   ("Clock", "GPU Core", vals[3]), ("Control", "GPU Fan", vals[4]),
                   ("SmallData", "GPU Memory Used", vals[5]), ("SmallData", "GPU Memory Total", vals[6])]
        return {"hw": "GpuNvidia", "name": name,
                "sensors": [{"type": t, "name": n, "value": v, "unit": UNITS[t]} for t, n, v in sensors]}

    def read(self):
        groups = []
        cpu = []
        if self.ps:
            per = self.ps.cpu_percent(percpu=True)
            cpu.append({"type": "Load", "name": "CPU Total", "value": sum(per) / max(1, len(per)), "unit": "%"})
            for i, v in enumerate(per, 1):
                cpu.append({"type": "Load", "name": f"CPU Core #{i}", "value": v, "unit": "%"})
            try:
                f = self.ps.cpu_freq()
                if f and f.current:
                    cpu.append({"type": "Clock", "name": "CPU (reported)", "value": float(f.current), "unit": "MHz"})
            except Exception:                        # noqa: BLE001
                pass
        t = self._acpi_temp()
        if t is not None:
            cpu.insert(0, {"type": "Temperature", "name": "ACPI thermal zone", "value": t, "unit": "°C"})
        if cpu:
            groups.append({"hw": "Cpu", "name": "Processor", "sensors": cpu})
        g = self._nvidia()
        if g:
            groups.append(g)
        if self.ps:
            vm = self.ps.virtual_memory()
            groups.append({"hw": "Memory", "name": "Memory", "sensors": [
                {"type": "Load", "name": "Memory", "value": vm.percent, "unit": "%"},
                {"type": "Data", "name": "Memory Used", "value": vm.used / 2**30, "unit": "GB"},
                {"type": "Data", "name": "Memory Available", "value": vm.available / 2**30, "unit": "GB"}]})
        sm = summarize(groups)
        if t is not None:
            sm["cpu_temp"] = t
            sm["cpu_temp_approx"] = True
        return {"groups": groups, "summary": sm}

    def close(self):
        pass


# ------------------------------------------------------------------ demo
class DemoBackend:
    """Synthetic sensors so the app can be previewed off Windows. stress_cpu / stress_gpu raise the load."""
    name = "demo"
    stress_cpu = False
    stress_gpu = False

    def __init__(self):
        self.t0 = time.time()
        self.cpu_t = 36.0
        self.gpu_t = 34.0
        self.note = "Demo mode: these readings are simulated."

    def read(self):
        t = time.time() - self.t0
        cl = 96.0 if DemoBackend.stress_cpu else 3 + 2 * abs(math.sin(t / 7))
        gl = 99.0 if DemoBackend.stress_gpu else 1 + abs(math.sin(t / 11))
        self.cpu_t += ((38 + cl * 0.5) - self.cpu_t) * 0.08 + random.uniform(-0.4, 0.4)
        self.gpu_t += ((33 + gl * 0.38) - self.gpu_t) * 0.05 + random.uniform(-0.2, 0.2)
        clock = 5050 if cl > 50 else 3600 + random.uniform(-400, 900)
        cores = [{"type": "Temperature", "name": f"Core #{i}", "value": self.cpu_t - random.uniform(0, 4), "unit": "°C"}
                 for i in range(1, 9)]
        cpu = [{"type": "Temperature", "name": "Core (Tctl/Tdie)", "value": self.cpu_t, "unit": "°C"}] + cores + [
            {"type": "Load", "name": "CPU Total", "value": cl, "unit": "%"},
            {"type": "Clock", "name": "Core #1", "value": clock, "unit": "MHz"},
            {"type": "Clock", "name": "Core #2", "value": clock - 25, "unit": "MHz"},
            {"type": "Power", "name": "Package", "value": 22 + cl * 0.9, "unit": "W"}]
        gpu = [{"type": "Temperature", "name": "GPU Core", "value": self.gpu_t, "unit": "°C"},
               {"type": "Temperature", "name": "GPU Hot Spot", "value": self.gpu_t + 11, "unit": "°C"},
               {"type": "Load", "name": "GPU Core", "value": gl, "unit": "%"},
               {"type": "Clock", "name": "GPU Core", "value": 2610 if gl > 50 else 210, "unit": "MHz"},
               {"type": "Power", "name": "GPU Package", "value": 18 + gl * 2.6, "unit": "W"},
               {"type": "Fan", "name": "GPU Fan 1", "value": 1650 if gl > 50 else 0, "unit": "RPM"}]
        mobo = [{"type": "Fan", "name": "CPU Fan", "value": 900 + cl * 7 + random.uniform(-15, 15), "unit": "RPM"},
                {"type": "Fan", "name": "System Fan #1", "value": 820 + random.uniform(-10, 10), "unit": "RPM"},
                {"type": "Fan", "name": "System Fan #2", "value": 0.0, "unit": "RPM"},
                {"type": "Temperature", "name": "System", "value": 31 + cl * 0.05, "unit": "°C"},
                {"type": "Voltage", "name": "+12V", "value": 12.07, "unit": "V"}]
        groups = [{"hw": "Cpu", "name": "AMD Ryzen 7 7800X3D", "sensors": cpu},
                  {"hw": "GpuNvidia", "name": "NVIDIA GeForce RTX 4070 SUPER", "sensors": gpu},
                  {"hw": "Memory", "name": "Generic Memory", "sensors": [
                      {"type": "Load", "name": "Memory", "value": 21 + cl * 0.1, "unit": "%"}]},
                  {"hw": "SuperIO", "name": "Nuvoton NCT6799D", "sensors": mobo},
                  {"hw": "Storage", "name": "Samsung SSD 990 PRO 2TB", "sensors": [
                      {"type": "Temperature", "name": "Temperature", "value": 38 + cl * 0.03, "unit": "°C"}]}]
        return {"groups": groups, "summary": summarize(groups)}

    def close(self):
        pass


def open_backend():
    """Best backend available. Returns (backend, note)."""
    if DEMO:
        b = DemoBackend()
        return b, b.note
    if lhm_available():
        try:
            b = LHMBackend()
            return b, b.note
        except Exception as e:                       # noqa: BLE001
            log(f"LibreHardwareMonitor failed to load: {e!r}")
    b = BasicBackend()
    return b, b.note
