"""
Download LibreHardwareMonitorLib (the sensor library) and its dependencies from NuGet into ./lhm,
using the .NET Framework builds that pythonnet loads. build_windows.bat runs this automatically.

    python fetch_lhm.py              # pinned version below
    set LHM_VERSION=0.9.7 & python fetch_lhm.py
"""

import io
import json
import os
import re
import shutil
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

LHM_VERSION = os.environ.get("LHM_VERSION", "0.9.6")
FEED = "https://api.nuget.org/v3-flatcontainer"
HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(HERE, "lhm")
LIB_PREF = ["net472", "net471", "net47", "net462", "net461", "net46", "net452", "net451", "net45",
            "netstandard2.0", "netstandard1.3", "netstandard1.1", "netstandard1.0"]
GROUP_PREF = [".NETFramework4.7.2", "net472", ".NETFramework4.7.1", ".NETFramework4.7", ".NETFramework4.6.2",
              ".NETFramework4.6.1", ".NETFramework4.6", ".NETFramework4.5", ".NETStandard2.0", "netstandard2.0"]
# Part of .NET Framework 4.7.2 already, or not used on Windows
SKIP = {"microsoft.netcore.platforms", "netstandard.library", "microsoft.win32.primitives", "mono.posix.netstandard",
        "microsoft.netcore.targets"}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "LandullBuildCheck-build"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def latest_stable(pkg):
    vers = json.loads(get(f"{FEED}/{pkg.lower()}/index.json"))["versions"]
    stable = [v for v in vers if "-" not in v]

    def key(v):
        return [int(x) if x.isdigit() else 0 for x in re.split(r"[.+]", v)]
    return max(stable, key=key)


def min_version(spec, pkg):
    v = re.sub(r"[\[\]\(\)\s]", "", spec or "").split(",")[0]
    return v or latest_stable(pkg)


def local(tag):
    return tag.rsplit("}", 1)[-1]


seen = {}


def fetch(pkg, version, depth=0):
    key = pkg.lower()
    if key in SKIP or key in seen:
        return
    seen[key] = version
    data = get(f"{FEED}/{key}/{version.lower()}/{key}.{version.lower()}.nupkg")
    z = zipfile.ZipFile(io.BytesIO(data))
    # DLLs grouped by the folder they sit in. Usually lib/<framework>/, but some packages use
    # runtimes/win[-x64]/lib/<framework>/ or other layouts, so look everywhere.
    folders = {}
    for orig in z.namelist():
        n = orig.replace("\\", "/")
        if n.lower().endswith(".dll") and "/" in n:
            folders.setdefault(n.rsplit("/", 1)[0], []).append(orig)

    def rank(folder):
        parts = folder.lower().split("/")
        fw = parts[-1]
        if fw not in LIB_PREF:
            return None
        if "runtimes" in parts and not any(p in ("win", "win-x64") for p in parts):
            return None                                  # linux/osx builds
        if "ref" in parts:
            return None                                  # reference assemblies, not loadable
        return (LIB_PREF.index(fw), 0 if "win-x64" in parts else 1 if "win" in parts else 2)

    ranked = sorted((r, f) for f in folders if (r := rank(f)) is not None)
    chosen = ranked[0][1] if ranked else None
    copied = []
    if chosen:
        for orig in folders[chosen]:
            name = orig.replace("\\", "/").rsplit("/", 1)[1]
            with z.open(orig) as src, open(os.path.join(DEST, name), "wb") as dst:
                shutil.copyfileobj(src, dst)
            copied.append(name)
    print(f"{'  ' * depth}{pkg} {version}  [{chosen or 'no .NET Framework build'}] {' '.join(copied)}")
    if depth == 0 and not chosen:
        print("  package contents:\n    " + "\n    ".join(z.namelist()))

    nuspec = next((o for o in z.namelist() if o.lower().endswith(".nuspec")), None)
    if not nuspec:
        return
    root = ET.fromstring(z.read(nuspec))
    deps_el = next((e for e in root.iter() if local(e.tag) == "dependencies"), None)
    if deps_el is None:
        return
    groups = [g for g in deps_el if local(g.tag) == "group"]
    if groups:
        by_fw = {g.get("targetFramework", ""): g for g in groups}
        group = next((by_fw[fw] for fw in GROUP_PREF if fw in by_fw), None)
        if group is None:
            group = next((g for fw, g in by_fw.items() if fw.lower().startswith((".netframework", "net4"))), None)
        deps = [d for d in group] if group is not None else []
    else:
        deps = [d for d in deps_el if local(d.tag) == "dependency"]
    for d in deps:
        if d.get("id"):
            fetch(d.get("id"), min_version(d.get("version"), d.get("id")), depth + 1)


def main():
    os.makedirs(DEST, exist_ok=True)
    print(f"Fetching LibreHardwareMonitorLib {LHM_VERSION} and dependencies into {DEST}")
    fetch("LibreHardwareMonitorLib", LHM_VERSION)
    with open(os.path.join(DEST, "NOTICE.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"LibreHardwareMonitorLib {LHM_VERSION} - Mozilla Public License 2.0\n"
                 "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor\n"
                 "The other DLLs here come from their NuGet packages under their own licenses:\n")
        for k, v in sorted(seen.items()):
            fh.write(f"  {k} {v}\n")
    if not os.path.isfile(os.path.join(DEST, "LibreHardwareMonitorLib.dll")):
        print("ERROR: LibreHardwareMonitorLib.dll was not found in the package.", file=sys.stderr)
        return 1
    print("OK: " + ", ".join(sorted(f for f in os.listdir(DEST) if f.endswith(".dll"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
