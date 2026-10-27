"""Shared helpers for Landull BuildCheck."""

import os
import subprocess
import sys
import time

APP_NAME = "Landull BuildCheck"
VERSION = "1.0.0"
GITHUB = "https://github.com/Landull"

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
DEMO = bool(os.environ.get("BUILDCHECK_DEMO"))   # fake sensor data, for previews on non-Windows machines


def base_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def asset(name):
    return os.path.join(base_dir(), "assets", name)


def lhm_dir():
    """Folder holding LibreHardwareMonitorLib.dll and its dependencies (added at build time)."""
    return os.path.join(base_dir(), "lhm")


def log_path():
    if not getattr(sys, "frozen", False):
        return os.path.join(base_dir(), "buildcheck.log")
    if IS_MAC:
        return os.path.expanduser("~/Library/Logs/LandullBuildCheck.log")
    return os.path.join(os.path.expanduser("~"), ".landull_buildcheck.log")


def log(msg):
    try:
        with open(log_path(), "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg).rstrip() + "\n")
    except OSError:
        pass


def no_window_flags():
    return 0x08000000 if IS_WIN else 0          # CREATE_NO_WINDOW


def run(cmd, timeout=60, input_text=None):
    """Run a command quietly. Returns (returncode, stdout, stderr); never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text,
                           encoding="utf-8", errors="replace", creationflags=no_window_flags())
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as e:                       # noqa: BLE001 - missing tool, timeout, etc.
        return -1, "", str(e)


def is_admin():
    if IS_WIN:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def worker_command(args):
    """Program + arguments that run a worker (benchmark / stress) through this same app."""
    if getattr(sys, "frozen", False):
        return sys.executable, ["--worker"] + args
    return sys.executable, [os.path.abspath(os.path.join(base_dir(), "buildcheck.py")), "--worker"] + args


def as_list(x):
    """PowerShell's ConvertTo-Json turns 1-item arrays into objects and empty ones into null."""
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def fmt_bytes(n, digits=1):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.{digits}f} {unit}"
        n /= 1024
    return "-"


def fmt_gb_decimal(n):
    """Drive sizes the way the box says them (1 TB = 10^12 bytes)."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    if n >= 1e12:
        return f"{n / 1e12:.2f} TB".replace(".00 ", " ")
    return f"{n / 1e9:.0f} GB"


def fmt_duration(sec):
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
