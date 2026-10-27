"""
System inventory and health checks.

On Windows, one PowerShell script gathers everything from CIM/WMI, the registry and the event
log and prints it as JSON. Display modes come from the Win32 API through ctypes. Other systems
get a smaller inventory so the app can still be previewed there.
"""

import json
import os
import platform
import re
import tempfile
import time

from bc_common import IS_MAC, IS_WIN, as_list, fmt_bytes, fmt_gb_decimal, is_admin, log, run

PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$o = [ordered]@{}
$errs = New-Object System.Collections.ArrayList
function Grab([string]$name, [scriptblock]$block) {
  try { $o[$name] = & $block } catch { $o[$name] = $null; [void]$errs.Add("$($name): $($_.Exception.Message)") }
}
function D($d) {
  if ($d) { try { return ([datetime]$d).ToString('yyyy-MM-dd HH:mm') } catch { return "$d" } }
  return $null
}
function Ev($filter, $max) {
  try {
    @(Get-WinEvent -FilterHashtable $filter -MaxEvents $max -ErrorAction Stop | ForEach-Object {
      [ordered]@{ t=(D $_.TimeCreated); id=$_.Id; level=$_.Level; provider=$_.ProviderName;
                  msg=(("$($_.Message)" -split "`r?`n")[0]) } })
  } catch {
    if ("$($_.Exception.Message)" -match 'No events') { @() } else { throw }
  }
}

Grab 'os' {
  $x = Get-CimInstance Win32_OperatingSystem
  $cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
  [ordered]@{ caption=$x.Caption; version=$x.Version; build=$x.BuildNumber; ubr=$cv.UBR; display=$cv.DisplayVersion;
              arch=$x.OSArchitecture; install=(D $x.InstallDate); boot=(D $x.LastBootUpTime); name=$x.CSName }
}
Grab 'cs' {
  $x = Get-CimInstance Win32_ComputerSystem
  [ordered]@{ manufacturer=$x.Manufacturer; model=$x.Model; ram=$x.TotalPhysicalMemory }
}
Grab 'board' { $x = Get-CimInstance Win32_BaseBoard; [ordered]@{ manufacturer=$x.Manufacturer; product=$x.Product; version=$x.Version } }
Grab 'bios'  { $x = Get-CimInstance Win32_BIOS; [ordered]@{ vendor=$x.Manufacturer; version=$x.SMBIOSBIOSVersion; date=(D $x.ReleaseDate) } }
Grab 'cpu' {
  @(Get-CimInstance Win32_Processor | ForEach-Object {
    [ordered]@{ name="$($_.Name)".Trim(); cores=$_.NumberOfCores; threads=$_.NumberOfLogicalProcessors; mhz=$_.MaxClockSpeed;
                virt=$_.VirtualizationFirmwareEnabled; l2=$_.L2CacheSize; l3=$_.L3CacheSize; socket=$_.SocketDesignation } })
}
Grab 'ram' {
  @(Get-CimInstance Win32_PhysicalMemory | ForEach-Object {
    [ordered]@{ size=$_.Capacity; rated=$_.Speed; configured=$_.ConfiguredClockSpeed; maker=$_.Manufacturer;
                part=("$($_.PartNumber)").Trim(); slot=$_.DeviceLocator; bank=$_.BankLabel; type=$_.SMBIOSMemoryType } })
}
Grab 'ramSlots' { (Get-CimInstance Win32_PhysicalMemoryArray | Measure-Object -Property MemoryDevices -Sum).Sum }
Grab 'gpu' {
  $vram = @{}
  Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}' -ErrorAction SilentlyContinue |
    ForEach-Object {
      try {
        $p = Get-ItemProperty $_.PSPath -ErrorAction Stop
        $m = $p.'HardwareInformation.qwMemorySize'
        if ($p.DriverDesc -and $m) { $vram[$p.DriverDesc] = [int64]$m }
      } catch {}
    }
  @(Get-CimInstance Win32_VideoController | ForEach-Object {
    [ordered]@{ name=$_.Name; ram=$_.AdapterRAM; vram=$vram[$_.Name]; driver=$_.DriverVersion; driverDate=(D $_.DriverDate);
                w=$_.CurrentHorizontalResolution; h=$_.CurrentVerticalResolution; hz=$_.CurrentRefreshRate;
                pnp=$_.PNPDeviceID; err=$_.ConfigManagerErrorCode } })
}
Grab 'disks' {
  $bootNums = @()
  try { $bootNums = @(Get-Partition -DriveLetter ($env:SystemDrive.TrimEnd(':')) | ForEach-Object { [string]$_.DiskNumber }) } catch {}
  @(Get-PhysicalDisk | ForEach-Object {
    $r = $null
    try { $r = $_ | Get-StorageReliabilityCounter -ErrorAction Stop } catch {}
    [ordered]@{ name=$_.FriendlyName; media="$($_.MediaType)"; bus="$($_.BusType)"; size=$_.Size; health="$($_.HealthStatus)";
                op="$($_.OperationalStatus)"; id="$($_.DeviceId)"; boot=($bootNums -contains "$($_.DeviceId)");
                temp=$r.Temperature; tempMax=$r.TemperatureMax; wear=$r.Wear; hours=$r.PowerOnHours;
                readErr=$r.ReadErrorsUncorrected; writeErr=$r.WriteErrorsUncorrected } })
}
Grab 'rawDisks' { @(Get-Disk | Where-Object { "$($_.PartitionStyle)" -eq 'RAW' } | ForEach-Object { [ordered]@{ n=$_.Number; name=$_.FriendlyName; size=$_.Size } }) }
Grab 'volumes' {
  @(Get-Volume | Where-Object { $_.DriveLetter -and "$($_.DriveType)" -eq 'Fixed' } | ForEach-Object {
    [ordered]@{ letter="$($_.DriveLetter)"; label=$_.FileSystemLabel; fs=$_.FileSystem; size=$_.Size; free=$_.SizeRemaining } })
}
Grab 'systemDrive' { $env:SystemDrive }
Grab 'problems' {
  @(Get-CimInstance Win32_PnPEntity -Filter 'ConfigManagerErrorCode <> 0' | ForEach-Object {
    [ordered]@{ name=$_.Name; code=$_.ConfigManagerErrorCode; cls=$_.PNPClass; id=$_.DeviceID } })
}
Grab 'activation' {
  $l = Get-CimInstance SoftwareLicensingProduct -Filter "ApplicationID='55c92734-d682-4d71-983e-d6ec3f16059f' AND PartialProductKey IS NOT NULL" |
       Select-Object -First 1
  if ($l) { [ordered]@{ status=$l.LicenseStatus; name=$l.Name } }
}
Grab 'lastUpdate' {
  $d = $null
  try { $d = (New-Object -ComObject Microsoft.Update.AutoUpdate).Results.LastInstallationSuccessDate } catch {}
  if (-not $d -or ([datetime]$d).Year -lt 2000) {
    $h = Get-HotFix | Where-Object { $_.InstalledOn } | Sort-Object InstalledOn -Descending | Select-Object -First 1
    $d = $h.InstalledOn
  }
  D $d
}
Grab 'pendingReboot' {
  $r = @()
  if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') { $r += 'Windows servicing' }
  if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') { $r += 'Windows Update' }
  $r -join ', '
}
Grab 'secureBoot' { try { [string](Confirm-SecureBootUEFI) } catch { "error: $($_.Exception.Message)" } }
Grab 'tpm' {
  $t = Get-Tpm
  $v = $null
  try { $v = (Get-CimInstance -Namespace root/cimv2/security/microsofttpm -ClassName Win32_Tpm -ErrorAction Stop).SpecVersion } catch {}
  [ordered]@{ present=$t.TpmPresent; ready=$t.TpmReady; enabled=$t.TpmEnabled; spec=$v }
}
Grab 'bitlocker' {
  $b = Get-BitLockerVolume -MountPoint $env:SystemDrive
  [ordered]@{ protection="$($b.ProtectionStatus)"; status="$($b.VolumeStatus)"; pct=$b.EncryptionPercentage }
}
Grab 'defender'   { $m = Get-MpComputerStatus; [ordered]@{ rtp=$m.RealTimeProtectionEnabled; av=$m.AntivirusEnabled; sig=(D $m.AntivirusSignatureLastUpdated) } }
Grab 'avProducts' { @(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct | ForEach-Object { $_.displayName }) }
Grab 'powerPlan'  { (powercfg /getactivescheme) -join ' ' }
Grab 'whea'       { Ev @{LogName='System'; ProviderName='Microsoft-Windows-WHEA-Logger'; StartTime=(Get-Date).AddDays(-14)} 100 }
Grab 'kp41'       { Ev @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; Id=41; StartTime=(Get-Date).AddDays(-14)} 50 }
Grab 'bugcheck'   { Ev @{LogName='System'; ProviderName='Microsoft-Windows-WER-SystemErrorReporting'; Id=1001; StartTime=(Get-Date).AddDays(-30)} 20 }
Grab 'monitors' {
  @(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorID | ForEach-Object {
    $n = ($_.UserFriendlyName | Where-Object { $_ -ne 0 } | ForEach-Object { [char]$_ }) -join ''
    $m = ($_.ManufacturerName | Where-Object { $_ -ne 0 } | ForEach-Object { [char]$_ }) -join ''
    [ordered]@{ name=$n; maker=$m } })
}
Grab 'net' {
  @(Get-NetAdapter -Physical | Where-Object { "$($_.Status)" -eq 'Up' } | ForEach-Object {
    [ordered]@{ name=$_.Name; desc=$_.InterfaceDescription; speed="$($_.LinkSpeed)"; media="$($_.PhysicalMediaType)" } })
}
Grab 'pawnio' { $s = Get-Service -Name PawnIO -ErrorAction SilentlyContinue; if ($s) { "$($s.Status)" } else { 'missing' } }
Grab 'winsat' {
  $w = Get-CimInstance Win32_WinSAT
  [ordered]@{ cpu=$w.CPUScore; mem=$w.MemoryScore; gfx=$w.GraphicsScore; d3d=$w.D3DScore; disk=$w.DiskScore;
              base=$w.WinSPRLevel; state=$w.WinSATAssessmentState }
}
$o['errors'] = @($errs)
$o | ConvertTo-Json -Depth 6 -Compress
"""

WINSAT_QUERY = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$w = Get-CimInstance Win32_WinSAT
[ordered]@{ cpu=$w.CPUScore; mem=$w.MemoryScore; gfx=$w.GraphicsScore; d3d=$w.D3DScore; disk=$w.DiskScore;
            base=$w.WinSPRLevel; state=$w.WinSATAssessmentState } | ConvertTo-Json -Compress
"""


def run_powershell(script, timeout=150):
    """Run a PowerShell script from a temp file and parse its JSON output."""
    fd, path = tempfile.mkstemp(suffix=".ps1", prefix="buildcheck_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig") as fh:
            fh.write(script)
        rc, out, err = run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-File", path], timeout=timeout)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    out = out.strip()
    start = out.find("{")
    if start < 0:
        raise RuntimeError(f"PowerShell returned no data (rc={rc}). {err.strip()[:300]}")
    return json.loads(out[start:])


# ------------------------------------------------------------------ display modes (Win32)
def windows_displays():
    """Each active display: adapter, current resolution/refresh, best refresh at that resolution."""
    import ctypes
    from ctypes import wintypes

    class DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32), ("DeviceString", wintypes.WCHAR * 128),
                    ("StateFlags", wintypes.DWORD), ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]

    class DEVMODEW(ctypes.Structure):
        _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                    ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
                    ("dmFields", wintypes.DWORD), ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG),
                    ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
                    ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                    ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32),
                    ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
                    ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                    ("dmDisplayFrequency", wintypes.DWORD), ("dmICMMethod", wintypes.DWORD),
                    ("dmICMIntent", wintypes.DWORD), ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                    ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                    ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD)]

    user32 = ctypes.WinDLL("user32")
    out = []
    i = 0
    while True:
        dd = DISPLAY_DEVICEW()
        dd.cb = ctypes.sizeof(dd)
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
            break
        i += 1
        if not dd.StateFlags & 0x1:                       # DISPLAY_DEVICE_ATTACHED_TO_DESKTOP
            continue
        cur = DEVMODEW()
        cur.dmSize = ctypes.sizeof(cur)
        if not user32.EnumDisplaySettingsW(dd.DeviceName, -1, ctypes.byref(cur)):   # ENUM_CURRENT_SETTINGS
            continue
        best_hz, max_w, max_h, mode = 0, 0, 0, 0
        while True:
            dm = DEVMODEW()
            dm.dmSize = ctypes.sizeof(dm)
            if not user32.EnumDisplaySettingsW(dd.DeviceName, mode, ctypes.byref(dm)):
                break
            mode += 1
            if dm.dmPelsWidth == cur.dmPelsWidth and dm.dmPelsHeight == cur.dmPelsHeight:
                best_hz = max(best_hz, dm.dmDisplayFrequency)
            if dm.dmPelsWidth * dm.dmPelsHeight > max_w * max_h:
                max_w, max_h = dm.dmPelsWidth, dm.dmPelsHeight
            if mode > 5000:
                break
        mon = DISPLAY_DEVICEW()
        mon.cb = ctypes.sizeof(mon)
        mon_name = ""
        if user32.EnumDisplayDevicesW(dd.DeviceName, 0, ctypes.byref(mon), 0):
            mon_name = mon.DeviceString
        out.append({"device": dd.DeviceName, "adapter": dd.DeviceString, "monitor": mon_name,
                    "w": cur.dmPelsWidth, "h": cur.dmPelsHeight, "hz": cur.dmDisplayFrequency,
                    "max_hz": best_hz, "max_w": max_w, "max_h": max_h, "primary": bool(dd.StateFlags & 0x4)})
    return out


# ------------------------------------------------------------------ helpers
def gpu_is_discrete(name):
    n = (name or "").lower()
    if "nvidia" in n or "geforce" in n or "quadro" in n:
        return True
    if "radeon" in n and re.search(r"\b(rx|pro|vii)\b", n):
        return True
    if re.search(r"\barc\(tm\)\s*[ab]\d|\barc [ab]\d", n):
        return True
    return False


def gpu_is_virtual(name):
    n = (name or "").lower()
    return any(k in n for k in ("basic display", "remote display", "parsec", "virtual", "meta", "spacedesk",
                                "displaylink", "citrix", "vmware", "hyper-v"))


def mem_type(code):
    return {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 29: "LPDDR3", 30: "LPDDR4", 34: "DDR5",
            35: "LPDDR5"}.get(code, "")


def parse_date(s):
    try:
        return time.mktime(time.strptime(s[:10], "%Y-%m-%d"))
    except Exception:
        return None


def days_since(s):
    t = parse_date(s or "")
    return None if t is None else (time.time() - t) / 86400


# ------------------------------------------------------------------ collection
def collect():
    """Gather the inventory. Slow on Windows (10-30 s); call it off the GUI thread."""
    info = {"collected": time.strftime("%Y-%m-%d %H:%M"), "platform": "windows" if IS_WIN else platform.system().lower(),
            "admin": is_admin(), "errors": []}
    if IS_WIN:
        try:
            raw = run_powershell(PS_SCRIPT)
        except Exception as e:                       # noqa: BLE001
            raw = {}
            info["errors"].append(f"inventory: {e}")
        info["errors"] += [str(x) for x in as_list(raw.get("errors"))]
        try:
            info["displays"] = windows_displays()
        except Exception as e:                       # noqa: BLE001
            info["displays"] = []
            info["errors"].append(f"displays: {e}")
        info.update(_shape_windows(raw))
        info["raw"] = raw
    else:
        info.update(_collect_generic())
    for e in info["errors"]:
        log("collect: " + e)
    return info


def _shape_windows(raw):
    os_ = raw.get("os") or {}
    cs = raw.get("cs") or {}
    board = raw.get("board") or {}
    bios = raw.get("bios") or {}
    cpus = as_list(raw.get("cpu"))
    cpu = cpus[0] if cpus else {}
    sticks = []
    for s in as_list(raw.get("ram")):
        sticks.append({"slot": s.get("slot") or "", "bank": s.get("bank") or "", "size": s.get("size") or 0,
                       "rated": s.get("rated") or 0, "configured": s.get("configured") or 0,
                       "maker": (s.get("maker") or "").strip(), "part": s.get("part") or "",
                       "type": mem_type(s.get("type"))})
    total_ram = sum(int(s["size"] or 0) for s in sticks) or cs.get("ram") or 0
    gpus = []
    for g in as_list(raw.get("gpu")):
        name = g.get("name") or "Unknown GPU"
        vram = g.get("vram") or g.get("ram") or 0
        gpus.append({"name": name, "vram": vram, "driver": g.get("driver") or "", "driver_date": g.get("driverDate") or "",
                     "res": f"{g.get('w')}x{g.get('h')}" if g.get("w") else "", "hz": g.get("hz"),
                     "active": bool(g.get("w")), "discrete": gpu_is_discrete(name), "virtual": gpu_is_virtual(name),
                     "err": g.get("err") or 0})
    disks = []
    for d in as_list(raw.get("disks")):
        disks.append({"name": d.get("name") or "Disk", "media": d.get("media") or "", "bus": d.get("bus") or "",
                      "size": d.get("size") or 0, "health": d.get("health") or "", "op": d.get("op") or "",
                      "boot": bool(d.get("boot")), "temp": d.get("temp"), "temp_max": d.get("tempMax"),
                      "wear": d.get("wear"), "hours": d.get("hours"), "read_err": d.get("readErr"),
                      "write_err": d.get("writeErr")})
    osname = (os_.get("caption") or "Windows").replace("Microsoft ", "")
    if os_.get("display"):
        osname += f" {os_['display']}"
    build = os_.get("build") or ""
    if os_.get("ubr"):
        build = f"{build}.{os_['ubr']}"
    return {
        "computer": {"name": os_.get("name") or "", "manufacturer": cs.get("manufacturer") or "",
                     "model": cs.get("model") or ""},
        "os": {"name": osname, "build": build, "arch": os_.get("arch") or "", "install": os_.get("install") or "",
               "boot": os_.get("boot") or ""},
        "cpu": {"name": cpu.get("name") or "Unknown CPU", "cores": cpu.get("cores"), "threads": cpu.get("threads"),
                "mhz": cpu.get("mhz"), "virt": cpu.get("virt"), "l2": cpu.get("l2"), "l3": cpu.get("l3"),
                "socket": cpu.get("socket") or "", "count": len(cpus)},
        "board": {"manufacturer": board.get("manufacturer") or "", "product": board.get("product") or "",
                  "version": board.get("version") or ""},
        "bios": {"vendor": bios.get("vendor") or "", "version": bios.get("version") or "", "date": bios.get("date") or ""},
        "ram": {"total": total_ram, "sticks": sticks, "slots": raw.get("ramSlots") or 0},
        "gpus": gpus,
        "disks": disks,
        "raw_disks": as_list(raw.get("rawDisks")),
        "volumes": as_list(raw.get("volumes")),
        "monitors": as_list(raw.get("monitors")),
        "net": as_list(raw.get("net")),
        "pawnio": raw.get("pawnio") or "unknown",
        "winsat": raw.get("winsat") or {},
    }


def _collect_generic():
    """Non-Windows preview: enough to fill the System page."""
    info = {"errors": []}
    try:
        import psutil
    except ImportError:
        psutil = None
    cpu = platform.processor() or platform.machine()
    if IS_MAC:
        rc, out, _ = run(["sysctl", "-n", "machdep.cpu.brand_string"], timeout=5)
        cpu = out.strip() or cpu
    else:
        try:
            with open("/proc/cpuinfo", encoding="utf-8") as fh:
                m = re.search(r"model name\s*:\s*(.+)", fh.read())
                cpu = m.group(1).strip() if m else cpu
        except OSError:
            pass
    total = 0
    if psutil:
        total = psutil.virtual_memory().total
    elif IS_MAC:
        rc, out, _ = run(["sysctl", "-n", "hw.memsize"], timeout=5)
        total = int(out.strip() or 0)
    gpus = []
    if IS_MAC:
        rc, out, _ = run(["system_profiler", "SPDisplaysDataType", "-json"], timeout=20)
        try:
            for g in json.loads(out).get("SPDisplaysDataType", []):
                gpus.append({"name": g.get("sppci_model") or g.get("_name") or "GPU", "vram": 0, "driver": "",
                             "driver_date": "", "res": "", "hz": None, "active": True, "discrete": False,
                             "virtual": False, "err": 0})
        except Exception:
            pass
    disks, volumes = [], []
    if psutil:
        for p in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(p.mountpoint)
            except OSError:
                continue
            volumes.append({"letter": p.mountpoint, "label": p.device, "fs": p.fstype, "size": u.total, "free": u.free})
    else:
        st = os.statvfs("/")
        volumes.append({"letter": "/", "label": "", "fs": "", "size": st.f_blocks * st.f_frsize,
                        "free": st.f_bavail * st.f_frsize})
    return {
        "computer": {"name": platform.node(), "manufacturer": "Apple" if IS_MAC else "", "model": ""},
        "os": {"name": f"{platform.system()} {platform.mac_ver()[0] if IS_MAC else platform.release()}",
               "build": platform.version()[:60], "arch": platform.machine(), "install": "", "boot": ""},
        "cpu": {"name": cpu, "cores": psutil.cpu_count(logical=False) if psutil else os.cpu_count(),
                "threads": os.cpu_count(), "mhz": None, "virt": None, "l2": None, "l3": None, "socket": "", "count": 1},
        "board": {"manufacturer": "", "product": "", "version": ""},
        "bios": {"vendor": "", "version": "", "date": ""},
        "ram": {"total": total, "sticks": [], "slots": 0},
        "gpus": gpus, "disks": disks, "raw_disks": [], "volumes": volumes, "monitors": [], "net": [],
        "displays": [], "pawnio": "n/a", "winsat": {}, "raw": {},
    }


def winsat_scores():
    if not IS_WIN:
        return {}
    try:
        return run_powershell(WINSAT_QUERY, timeout=60)
    except Exception as e:                           # noqa: BLE001
        log(f"winsat query: {e}")
        return {}


# ------------------------------------------------------------------ health checks
PASS, WARN, FAIL, INFO, SKIP = "PASS", "WARN", "FAIL", "INFO", "SKIP"
ORDER = {FAIL: 0, WARN: 1, PASS: 2, INFO: 3, SKIP: 4}
KNOWN_SPEEDS = {2133, 2400, 2666, 2933, 3000, 3200, 3333, 3466, 3600, 3733, 3800, 3866, 4000, 4133, 4266, 4400,
                4600, 4800, 5200, 5600, 6000, 6200, 6400, 6600, 6800, 7000, 7200, 7600, 8000, 8200, 8400}
JEDEC = {2133, 2400, 2666, 2933, 3200, 4800, 5200, 5600}


def _chk(cat, title, status, detail, fix=""):
    return {"cat": cat, "title": title, "status": status, "detail": detail, "fix": fix}


def _kit_speed(part):
    """Rated speed hidden in a RAM kit's part number, e.g. CMK32GX5M2B6000C36 -> 6000."""
    for m in re.finditer(r"(\d{4})", part or ""):
        v = int(m.group(1))
        if v in KNOWN_SPEEDS:
            return v
    return None


def _channel(stick):
    text = f"{stick.get('bank', '')} {stick.get('slot', '')}".upper()
    for pat in (r"CHANNEL\s*([A-H])", r"DIMM[_ ]?([A-H])\d", r"\b([A-H])[12]\b", r"CHANNEL([A-H])"):
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


def _link_mbps(speed):
    m = re.match(r"([\d.]+)\s*([GMK])bps", speed or "")
    if not m:
        return None
    return float(m.group(1)) * {"G": 1000, "M": 1, "K": 0.001}[m.group(2)]


def build_checks(info, ctx=None):
    """Turn the inventory (plus sensor / stress / benchmark results in ctx) into pass/warn/fail checks."""
    ctx = ctx or {}
    out = []
    win = info.get("platform") == "windows"
    raw = info.get("raw") or {}
    errors = [str(e) for e in info.get("errors", [])]

    def got(name):
        """True when the inventory section ran without error (an empty result still counts)."""
        return name in raw and not any(e.startswith(name + ":") for e in errors)

    # ---------------- hardware
    sticks = info.get("ram", {}).get("sticks", [])
    if sticks:
        cfg = min((s["configured"] or 0) for s in sticks) or None
        kit = max((_kit_speed(s["part"]) or 0) for s in sticks) or None
        if cfg and kit and kit > cfg + 100:
            out.append(_chk("Hardware", "Memory runs at its rated speed", WARN,
                            f"RAM is running at {cfg} MT/s, but the kit ({sticks[0]['part']}) is rated for {kit} MT/s.",
                            "Enable XMP (Intel) or EXPO (AMD) in the BIOS, then re-run the stress test."))
        elif cfg and kit:
            out.append(_chk("Hardware", "Memory runs at its rated speed", PASS, f"{cfg} MT/s, matching the kit's rating."))
        elif cfg:
            st = INFO if cfg in JEDEC else PASS
            out.append(_chk("Hardware", "Memory runs at its rated speed", st,
                            f"Running at {cfg} MT/s." + (" That's a stock JEDEC speed." if cfg in JEDEC else ""),
                            "If the kit is rated faster, enable XMP/EXPO in the BIOS." if cfg in JEDEC else ""))
        n = len(sticks)
        chans = [_channel(s) for s in sticks]
        if n == 1:
            out.append(_chk("Hardware", "Memory is dual-channel", WARN, "Only one memory stick is installed (single-channel).",
                            "Two matched sticks give roughly double the memory bandwidth."))
        elif n == 2 and all(chans) and len(set(chans)) == 1:
            out.append(_chk("Hardware", "Memory is dual-channel", WARN,
                            f"Both sticks are on channel {chans[0]} ({', '.join(s['slot'] for s in sticks)}).",
                            "Move one stick to the other channel. Most boards want slots A2 + B2 - check the manual."))
        elif n >= 2:
            where = ", ".join(s["slot"] for s in sticks if s["slot"])
            out.append(_chk("Hardware", "Memory is dual-channel", PASS, f"{n} sticks installed ({where})."))

    if win:
        probs = as_list(raw.get("problems"))
        if not got("problems"):
            out.append(_chk("Drivers", "Device Manager has no problems", SKIP, "Couldn't read Device Manager."))
        elif probs:
            names = "; ".join(f"{p.get('name') or p.get('id')} (code {p.get('code')})" for p in probs[:8])
            missing = sum(1 for p in probs if p.get("code") == 28)
            out.append(_chk("Drivers", "Device Manager has no problems", FAIL,
                            f"{len(probs)} device(s) with problems: {names}." +
                            (f" {missing} {'has' if missing == 1 else 'have'} no driver installed." if missing else ""),
                            "Install the chipset driver from the motherboard maker's support page, then run Windows Update."))
        else:
            out.append(_chk("Drivers", "Device Manager has no problems", PASS, "Every device has a working driver."))

        gpus = info.get("gpus", [])
        basic = [g for g in gpus if "basic display" in g["name"].lower()]
        real = [g for g in gpus if not g["virtual"]]
        if basic and not [g for g in real if "basic display" not in g["name"].lower()]:
            out.append(_chk("Drivers", "Graphics driver installed", FAIL, "Windows is using the Microsoft Basic Display Adapter.",
                            "Install the NVIDIA / AMD / Intel graphics driver."))
        elif real:
            disc = [g for g in real if g["discrete"]] or real
            g = disc[0]
            age = days_since(g["driver_date"])
            if age is not None and age > 365:
                out.append(_chk("Drivers", "Graphics driver installed", WARN,
                                f"{g['name']} driver {g['driver']} is from {g['driver_date'][:10]} ({int(age // 30)} months old).",
                                "Update to the latest driver from the GPU maker."))
            else:
                out.append(_chk("Drivers", "Graphics driver installed", PASS,
                                f"{g['name']}, driver {g['driver']}" + (f" ({g['driver_date'][:10]})" if g["driver_date"] else "") + "."))

        displays = info.get("displays", [])
        discrete = [g for g in gpus if g["discrete"]]
        if displays and discrete:
            on_igpu = [d for d in displays if not gpu_is_discrete(d["adapter"]) and not gpu_is_virtual(d["adapter"])]
            if on_igpu and len(on_igpu) == len(displays):
                out.append(_chk("Hardware", "Monitor is plugged into the graphics card", FAIL,
                                f"Every display runs on {on_igpu[0]['adapter']}, not the {discrete[0]['name']}.",
                                "Move the display cable from the motherboard's video port to the graphics card."))
            elif on_igpu:
                out.append(_chk("Hardware", "Monitor is plugged into the graphics card", WARN,
                                f"{len(on_igpu)} display(s) run on the integrated graphics ({on_igpu[0]['adapter']}).",
                                "Plug gaming displays into the graphics card."))
            else:
                out.append(_chk("Hardware", "Monitor is plugged into the graphics card", PASS,
                                f"Displays run on the {discrete[0]['name']}."))
        for i, d in enumerate(displays, 1):
            label = d.get("monitor") or f"Display {i}"
            if d["max_hz"] and d["max_hz"] > d["hz"] + 1:
                out.append(_chk("Hardware", f"{label}: refresh rate", WARN,
                                f"Running at {d['hz']} Hz, but it supports {d['max_hz']} Hz at {d['w']}x{d['h']}.",
                                "Settings > System > Display > Advanced display > Choose a refresh rate."))
            else:
                out.append(_chk("Hardware", f"{label}: refresh rate", PASS, f"{d['w']}x{d['h']} at {d['hz']} Hz (the highest available)."))

        disks = info.get("disks", [])
        boot = [d for d in disks if d["boot"]]
        if boot:
            b = boot[0]
            if b["media"].upper() == "HDD":
                out.append(_chk("Storage", "Windows is on an SSD", WARN, f"Windows is installed on a hard drive ({b['name']}).",
                                "Install Windows on the SSD for a much faster system."))
            else:
                out.append(_chk("Storage", "Windows is on an SSD", PASS, f"{b['name']} ({b['bus']} {b['media']})."))
        bad = [d for d in disks if d["health"] and d["health"].lower() != "healthy"]
        errs = [d for d in disks if (d["read_err"] or 0) > 0 or (d["write_err"] or 0) > 0]
        worn = [d for d in disks if (d["wear"] or 0) >= 10]
        if not disks:
            out.append(_chk("Storage", "Drive health", SKIP, "Couldn't read drive health."))
        elif bad or errs:
            names = ", ".join(f"{d['name']} ({d['health']})" for d in bad) or ", ".join(d["name"] for d in errs)
            out.append(_chk("Storage", "Drive health", FAIL, f"Problem reported by: {names}.",
                            "Back up and replace the drive (it's new - RMA it)."))
        elif worn:
            out.append(_chk("Storage", "Drive health", WARN,
                            "; ".join(f"{d['name']} shows {d['wear']}% wear" for d in worn) + ".",
                            "High wear on a new build can mean a used or refurbished drive."))
        else:
            out.append(_chk("Storage", "Drive health", PASS, f"{len(disks)} drive(s) report Healthy."))
        rawd = info.get("raw_disks", [])
        if rawd:
            out.append(_chk("Storage", "All drives are set up", WARN,
                            "; ".join(f"Disk {r.get('n')} {r.get('name')} ({fmt_gb_decimal(r.get('size'))})" for r in rawd) +
                            " isn't initialized, so it won't show up in File Explorer.",
                            "Open Disk Management, initialize it as GPT, and create a volume."))
        elif disks:
            out.append(_chk("Storage", "All drives are set up", PASS, "Every drive is initialized."))

        net = info.get("net", [])
        if net:
            slow = [n for n in net if (_link_mbps(n.get("speed")) or 1000) < 1000 and "802.3" in (n.get("media") or "802.3")
                    and "wi" not in (n.get("desc") or "").lower()]
            detail = "; ".join(f"{n.get('name')}: {n.get('speed')}" for n in net)
            if slow:
                out.append(_chk("Hardware", "Network connected", WARN, detail + ".",
                                "A wired link below 1 Gbps usually means a bad cable or port."))
            else:
                out.append(_chk("Hardware", "Network connected", PASS, detail + "."))
        else:
            out.append(_chk("Hardware", "Network connected", WARN, "No network adapter is connected.",
                            "Install the LAN / Wi-Fi driver and connect a network."))

        # ---------------- windows
        act = raw.get("activation") or {}
        if act:
            st = act.get("status")
            out.append(_chk("Windows", "Windows is activated", PASS if st == 1 else WARN,
                            "Activated." if st == 1 else f"Not activated (license status {st}).",
                            "" if st == 1 else "Settings > System > Activation."))
        upd = raw.get("lastUpdate")
        if upd:
            age = days_since(upd)
            ok = age is not None and age <= 30
            out.append(_chk("Windows", "Windows Update is current", PASS if ok else WARN,
                            f"Last update installed {upd[:10]}" + (f" ({int(age)} days ago)." if age is not None else "."),
                            "" if ok else "Run Windows Update until it says you're up to date."))
        pend = raw.get("pendingReboot")
        if pend:
            out.append(_chk("Windows", "No restart pending", WARN, f"A restart is pending ({pend}).",
                            "Restart to finish installing updates."))
        elif got("pendingReboot"):
            out.append(_chk("Windows", "No restart pending", PASS, "Nothing is waiting on a restart."))
        plan = raw.get("powerPlan") or ""
        m = re.search(r"\(([^)]+)\)", plan)
        if m:
            name = m.group(1)
            out.append(_chk("Windows", "Power plan", WARN if "saver" in name.lower() else INFO, f"Active plan: {name}.",
                            "Switch to Balanced or High performance." if "saver" in name.lower() else ""))

        # ---------------- security
        sb = str(raw.get("secureBoot") or "")
        if sb == "True":
            out.append(_chk("Security", "Secure Boot", PASS, "Enabled."))
        elif sb == "False":
            out.append(_chk("Security", "Secure Boot", WARN, "Supported but turned off.",
                            "Enable Secure Boot in the BIOS (Windows 11 and some anti-cheat games need it)."))
        elif "not supported" in sb.lower():
            out.append(_chk("Security", "Secure Boot", WARN, "The board is booting in Legacy/CSM mode.",
                            "Switch the BIOS to UEFI mode (needs a GPT boot drive)."))
        elif sb:
            out.append(_chk("Security", "Secure Boot", SKIP, "Couldn't read Secure Boot state (run as administrator)."))
        tpm = raw.get("tpm") or {}
        if tpm:
            spec = (tpm.get("spec") or "").split(",")[0].strip()
            if tpm.get("present") and tpm.get("ready"):
                out.append(_chk("Security", "TPM", PASS, f"Present and ready{' (TPM ' + spec + ')' if spec else ''}."))
            else:
                out.append(_chk("Security", "TPM", WARN, "No TPM, or it isn't ready.",
                                "Enable fTPM (AMD) or PTT (Intel) in the BIOS."))
        dfd = raw.get("defender") or {}
        avs = [a for a in as_list(raw.get("avProducts")) if a]
        if dfd.get("rtp") or [a for a in avs if "defender" not in a.lower()]:
            out.append(_chk("Security", "Antivirus is on", PASS, ", ".join(avs) or "Microsoft Defender real-time protection is on."))
        elif dfd or avs:
            out.append(_chk("Security", "Antivirus is on", WARN, "Real-time protection is off.",
                            "Windows Security > Virus & threat protection."))
        bl = raw.get("bitlocker") or {}
        if bl:
            on = bl.get("protection") in ("On", "1")
            out.append(_chk("Security", "BitLocker / device encryption", INFO,
                            f"{'On' if on else 'Off'} for the Windows drive ({bl.get('status')}).",
                            "Make sure the owner has the recovery key (account.microsoft.com/devices/recoverykey)." if on else ""))
        virt = info.get("cpu", {}).get("virt")
        if virt is not None:
            out.append(_chk("Security", "CPU virtualization", INFO, "Enabled in firmware." if virt else "Turned off in the BIOS.",
                            "" if virt else "Enable SVM (AMD) or VT-x (Intel) if the owner needs WSL, Docker or VMs."))

        # ---------------- stability
        whea = as_list(raw.get("whea"))
        if got("whea"):
            errs_ = [e for e in whea if e.get("level") in (1, 2)]
            if errs_:
                out.append(_chk("Stability", "No hardware errors (WHEA)", FAIL,
                                f"{len(errs_)} hardware error(s) in the last 14 days. Latest: {errs_[0].get('msg', '')[:140]}",
                                "Often an unstable memory profile or overclock, a bad riser cable, or failing hardware. "
                                "Load BIOS defaults and re-test."))
            elif whea:
                out.append(_chk("Stability", "No hardware errors (WHEA)", WARN,
                                f"{len(whea)} corrected hardware warning(s) in the last 14 days.",
                                "Usually PCIe link errors; try a different slot or update the BIOS and chipset driver."))
            else:
                out.append(_chk("Stability", "No hardware errors (WHEA)", PASS, "None in the last 14 days."))
        kp = as_list(raw.get("kp41"))
        if got("kp41"):
            out.append(_chk("Stability", "No unexpected shutdowns", WARN if kp else PASS,
                            f"{len(kp)} unexpected power loss/reset(s) in the last 14 days." if kp else "None in the last 14 days.",
                            "Expected if you power-cycled during the build; otherwise check the PSU and power cables." if kp else ""))
        bc = as_list(raw.get("bugcheck"))
        if got("bugcheck"):
            out.append(_chk("Stability", "No blue screens", FAIL if bc else PASS,
                            f"{len(bc)} blue screen(s) in the last 30 days. Latest: {bc[0].get('msg', '')[:140]}" if bc
                            else "None in the last 30 days.",
                            "Check memory stability (disable XMP to test) and drivers." if bc else ""))

    # ---------------- thermals
    idle = ctx.get("idle") or {}
    note = ctx.get("sensor_note") or "Temperature sensors aren't available."
    ct = idle.get("cpu_temp")
    if ct is not None:
        st = PASS if ct <= 55 else WARN if ct <= 70 else FAIL
        out.append(_chk("Thermals", "CPU temperature at idle", st, f"{ct:.0f} °C.",
                        "" if st == PASS else "Check cooler mounting, thermal paste coverage, and that the pump/fans are connected."))
    else:
        out.append(_chk("Thermals", "CPU temperature at idle", SKIP, note))
    gt = idle.get("gpu_temp")
    if gt is not None:
        st = PASS if gt <= 60 else WARN if gt <= 75 else FAIL
        out.append(_chk("Thermals", "GPU temperature at idle", st, f"{gt:.0f} °C.",
                        "" if st == PASS else "Check case airflow and that the GPU fans spin under load."))
    fans = ctx.get("fans") or []
    if fans:
        stopped = [f for f in fans if not f[1]]
        spinning = [f for f in fans if f[1]]
        detail = ", ".join(f"{n}: {v:.0f} RPM" for n, v in spinning[:8])
        if spinning:
            out.append(_chk("Thermals", "Fans are spinning", PASS if not stopped else INFO,
                            detail + (f". {len(stopped)} header(s) read 0 RPM." if stopped else "."),
                            "0 RPM is normal for empty headers or fans in zero-RPM mode." if stopped else ""))
        else:
            out.append(_chk("Thermals", "Fans are spinning", WARN, "Every fan header reads 0 RPM.",
                            "Make sure the fans are plugged into the motherboard headers."))
    stress = ctx.get("stress")
    if stress:
        out.append(_chk("Thermals", "Stress test", stress["status"], stress["summary"], stress.get("fix", "")))
    else:
        out.append(_chk("Thermals", "Stress test", SKIP, "Not run yet."))
    bench = ctx.get("bench")
    if bench:
        out.append(_chk("Performance", "Benchmarks", INFO, bench.get("summary", "Completed.")))

    out.sort(key=lambda c: (CAT_ORDER.get(c["cat"], 9), ORDER.get(c["status"], 9)))
    return out


CAT_ORDER = {"Hardware": 0, "Drivers": 1, "Storage": 2, "Thermals": 3, "Stability": 4, "Windows": 5, "Security": 6,
             "Performance": 7}


def verdict(checks):
    fails = sum(1 for c in checks if c["status"] == FAIL)
    warns = sum(1 for c in checks if c["status"] == WARN)
    if fails:
        return FAIL, f"Needs attention - {fails} failed, {warns} warning(s)"
    if warns:
        return WARN, f"Passed with {warns} warning(s)"
    return PASS, "Passed"


def spec_rows(info):
    """(label, value) pairs used by the System page and the report."""
    cpu = info.get("cpu", {})
    ram = info.get("ram", {})
    rows = [("CPU", cpu.get("name", "-") + (f"  ·  {cpu.get('cores')} cores / {cpu.get('threads')} threads"
                                                 if cpu.get("cores") else ""))]
    for g in info.get("gpus", []):
        if g["virtual"]:
            continue
        rows.append(("GPU", g["name"] + (f"  ·  {fmt_bytes(g['vram'], 0)}" if g["vram"] and g["vram"] > 2**28 else "")))
    sticks = ram.get("sticks", [])
    detail = ""
    if sticks:
        sizes = {fmt_bytes(s["size"], 0) for s in sticks}
        t = sticks[0]["type"]
        detail = f"  ·  {len(sticks)} x {'/'.join(sorted(sizes))} {t} @ {sticks[0]['configured'] or '?'} MT/s"
    rows.append(("Memory", fmt_bytes(ram.get("total"), 0) + detail))
    b = info.get("board", {})
    if b.get("product"):
        rows.append(("Motherboard", f"{b.get('manufacturer', '')} {b.get('product', '')}".strip()))
    bi = info.get("bios", {})
    if bi.get("version"):
        rows.append(("BIOS", f"{bi.get('version')}" + (f" ({bi['date'][:10]})" if bi.get("date") else "")))
    for d in info.get("disks", []):
        rows.append(("Storage", f"{d['name']}  ·  {fmt_gb_decimal(d['size'])} {d['bus']} {d['media']}".strip()
                     + ("  ·  Windows" if d["boot"] else "")))
    for i, d in enumerate(info.get("displays", []), 1):
        rows.append(("Display", f"{d.get('monitor') or f'Display {i}'}  ·  {d['w']}x{d['h']} @ {d['hz']} Hz"))
    o = info.get("os", {})
    rows.append(("OS", f"{o.get('name', '')}  ·  build {o.get('build', '')}".strip(" ·")))
    return rows
