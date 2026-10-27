# Downloads LibreHardwareMonitorLib (the sensor library) and its dependencies from NuGet into .\lhm
# (the .NET Framework 4.7.2 builds, which pythonnet loads). build_windows.bat runs this automatically.
# Pin a version with:  $env:LHM_VERSION = "0.9.6"
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dest = Join-Path $root 'lhm'
$work = Join-Path $env:TEMP ("lhm_" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $dest, $work | Out-Null
$feed = 'https://api.nuget.org/v3-flatcontainer'
$frameworks = @('net472', 'net471', 'net47', 'net462', 'net461', 'net46', 'net452', 'net45', 'netstandard2.0', 'netstandard1.3')
# Already part of .NET Framework 4.7.2 - no need to ship these
$skip = @('microsoft.netcore.platforms', 'netstandard.library', 'microsoft.win32.primitives')
$seen = @{}

function Get-LatestStable([string]$id) {
  $idx = Invoke-RestMethod "$feed/$($id.ToLower())/index.json"
  $stable = $idx.versions | Where-Object { $_ -notmatch '-' } | Sort-Object { [version]($_ -replace '^(\d+(\.\d+){0,3}).*', '$1') }
  return ($stable | Select-Object -Last 1)
}

function Get-Package([string]$id, [string]$version) {
  $key = $id.ToLower()
  if ($seen.ContainsKey($key) -or $skip -contains $key) { return }
  $seen[$key] = $version
  $file = Join-Path $work "$key.$version.zip"
  Write-Host "  $id $version"
  Invoke-WebRequest "$feed/$key/$version/$key.$version.nupkg" -OutFile $file -UseBasicParsing
  $dir = Join-Path $work "$key.$version"
  Expand-Archive $file -DestinationPath $dir -Force
  foreach ($fw in $frameworks) {
    $lib = Join-Path $dir "lib\$fw"
    if (Test-Path $lib) {
      Get-ChildItem $lib -Filter *.dll | Copy-Item -Destination $dest -Force
      break
    }
  }
  # follow the dependencies declared for .NET Framework
  $nuspec = Get-ChildItem $dir -Filter *.nuspec | Select-Object -First 1
  [xml]$x = Get-Content $nuspec.FullName
  $groups = @($x.package.metadata.dependencies.group)
  $group = $null
  foreach ($fw in @('.NETFramework4.7.2', '.NETFramework4.7.1', '.NETFramework4.7', '.NETFramework4.6.2', '.NETFramework4.6.1',
                    '.NETFramework4.5', '.NETStandard2.0')) {
    $group = $groups | Where-Object { $_.targetFramework -eq $fw } | Select-Object -First 1
    if ($group) { break }
  }
  $deps = if ($group) { @($group.dependency) } else { @($x.package.metadata.dependencies.dependency) }
  foreach ($d in $deps) {
    if (-not $d -or -not $d.id) { continue }
    $v = ($d.version -replace '[\[\]\(\)\s]', '').Split(',')[0]
    if (-not $v) { $v = Get-LatestStable $d.id }
    Get-Package $d.id $v
  }
}

$ver = $env:LHM_VERSION
if (-not $ver) { $ver = Get-LatestStable 'LibreHardwareMonitorLib' }
Write-Host "Fetching LibreHardwareMonitorLib $ver and dependencies:"
Get-Package 'LibreHardwareMonitorLib' $ver
Set-Content -Path (Join-Path $dest 'NOTICE.txt') -Encoding UTF8 -Value @"
LibreHardwareMonitorLib $ver - Mozilla Public License 2.0
https://github.com/LibreHardwareMonitor/LibreHardwareMonitor
Other files in this folder come from their NuGet packages under their own licenses.
"@
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
if (-not (Test-Path (Join-Path $dest 'LibreHardwareMonitorLib.dll'))) { throw 'LibreHardwareMonitorLib.dll was not found in the package.' }
Write-Host "OK -> $dest"
