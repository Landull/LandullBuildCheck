# Landull BuildCheck

A post-build health check for Windows PCs. Run it on a fresh build to see the specs, watch live temperatures, stress the CPU and GPU, run quick benchmarks, catch common build mistakes, and hand the owner a branded PDF report.

It shares its look with [Landull Toolkit](https://github.com/Landull/LandullToolkit).

## Download

Get `LandullBuildCheck.exe` from the [**Releases**](https://github.com/Landull/LandullBuildCheck/releases/latest) page. It's a single file, so you can keep it on the USB stick you use for builds. Nothing needs to be installed.

**First launch:**

- The app asks for administrator access. It needs that to read CPU, motherboard and drive sensors.
- It isn't code-signed, so SmartScreen warns you the first time. Click **More info** › **Run anyway**.
- CPU and motherboard temperatures need the free [PawnIO](https://pawnio.eu) driver. If it's missing, the Sensors page offers to install it with winget (`namazso.PawnIO`). GPU and drive sensors work without it.

## What it does

| Page | |
|---|---|
| **Home** | Live CPU/GPU temperatures and loads, a summary of the PC, and **Run full check**: system info → idle temperatures → benchmarks → stress test → health checks → PDF report, in one click. |
| **System** | CPU, motherboard and BIOS, graphics cards and drivers, every memory stick (slot, speed, part number), drives with health/wear/temperature, volumes, displays and refresh rates, network links, Windows edition and build. |
| **Sensors** | Every sensor LibreHardwareMonitor finds (temperatures, clocks, loads, power, fans, voltages) with min/max, plus a 5-minute temperature graph. |
| **Stress test** | Loads every CPU thread with compression work that checks its own results, and opens a GPU render window that adapts its load. Tracks max/average temperatures, throttling, clock drops and calculation errors, and stops automatically if anything gets dangerously hot. |
| **Benchmarks** | CPU single- and multi-core scores, memory copy bandwidth, drive sequential read/write and 4K random reads (bypassing the Windows cache), and optional WinSAT scores (includes graphics). |
| **Health checks** | Pass / warn / fail checks for the mistakes that show up after a build (see below). |
| **Report** | Customer, build and notes fields, then **Export PDF**. Reports save to `Documents\Landull BuildCheck`. |

### Health checks

- **Hardware:** XMP/EXPO off (running speed vs. the kit's rated speed from its part number) · single-channel memory or both sticks on one channel · monitor plugged into the motherboard instead of the graphics card · display stuck below its max refresh rate · wired network below 1 Gbps
- **Drivers:** devices with problems in Device Manager (missing chipset drivers, etc.) · Microsoft Basic Display Adapter or an old GPU driver
- **Storage:** Windows on a hard drive · drive health, wear and uncorrected errors · drives that were never initialized (invisible in File Explorer)
- **Thermals:** idle CPU/GPU temperatures · fans spinning · stress test result
- **Stability:** WHEA hardware errors · unexpected shutdowns · blue screens
- **Windows:** activation · last update · pending restart · power plan
- **Security:** Secure Boot · TPM · antivirus · BitLocker (reminds you about the recovery key) · CPU virtualization

## Build it yourself

You need Windows and Python 3.10–3.13.

1. Double-click `build_windows.bat`.
2. The app is built to `dist\LandullBuildCheck.exe`.

The build script downloads the LibreHardwareMonitor sensor library from NuGet into `lhm\` (see `fetch_lhm.ps1`). Pushing a tag such as `v1.0.0` makes GitHub Actions build the exe, test that the sensors load, and publish a release.

**Run from source:** `pip install -r requirements.txt`, then `python buildcheck.py` (as administrator for full sensors).

**Preview on a Mac or Linux:** `BUILDCHECK_DEMO=1 python3 buildcheck.py` runs with simulated sensors. Most health checks are Windows-only.

**Debugging:** `LandullBuildCheck.exe --selftest out.json` writes the inventory, sensor list and checks to a JSON file. The app also logs to `%USERPROFILE%\.landull_buildcheck.log`.

## Notes

- Benchmark scores are for comparing BuildCheck runs with each other (same build, before/after, one build vs. another). They aren't Cinebench or 3DMark numbers.
- The CPU stress load is lighter than Prime95 small-FFT or OCCT AVX tests. For overclocked systems, follow up with one of those.
- The drive test writes a temporary 1 GB (or 4 GB) file to the drive you pick and deletes it afterwards.
- Sensors come from [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) (MPL 2.0).

---
Part of [Landull](https://github.com/Landull)'s tools.
