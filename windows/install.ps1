<#
  aqd one-command installer for Windows.  (messages are ASCII-only on purpose: Windows PowerShell 5.1
  reads a .ps1 without a BOM as the ANSI code page, which mangles non-ASCII text; a BOM would break
  `irm | iex`. ASCII keeps every invocation path working.)

  FRESH install (paste ONE line in PowerShell, no parameters):
    irm https://raw.githubusercontent.com/truonglh2372/script_google_map/main/windows/install.ps1 | iex

  CLEAN reinstall of a machine that already ran an older copy (stops what runs, reports, backs up,
  wipes the conflicting state, then installs again) - still ONE line:
    $env:AQD_CLEAN=1; irm https://raw.githubusercontent.com/truonglh2372/script_google_map/main/windows/install.ps1 | iex

  Install somewhere else (tests):
    $env:AQD_ROOT='C:\aqd_test'; irm https://raw.githubusercontent.com/truonglh2372/script_google_map/main/windows/install.ps1 | iex

  Install the payload of another branch/tag (testing a change before merging it):
    $env:AQD_REPO_REF='feature/1-one-command-installer'; irm <the same url> | iex

  Same thing without env vars, when called as a script block:
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/truonglh2372/script_google_map/main/windows/install.ps1))) -Root C:\aqd_test -Clean -NoStart -Yes

  Switches: -Root <dir>  -Clean  -NoStart (install only, do not launch alt.py)  -NoChrome  -SkipHeavy  -Yes (accept defaults)
#>
[CmdletBinding()]
param(
  [string]$Root,
  [switch]$Clean,
  [switch]$NoStart,
  [switch]$NoChrome,
  [switch]$SkipHeavy,
  [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$DefaultRoot = 'C:\aqd'
$RawBase = 'https://raw.githubusercontent.com/truonglh2372/script_google_map/main'
$RepoGit = 'https://github.com/truonglh2372/script_google_map.git'
$RepoZip = 'https://codeload.github.com/truonglh2372/script_google_map/zip/refs/heads/main'
$RequiredPkgs = @('requests', 'pillow', 'playwright', 'openlocationcode')
$HeavyPkgs = @('open_clip_torch', 'torch', 'easyocr')

function Say([string]$m) { Write-Host $m }
function Head([string]$m) { Write-Host ""; Write-Host "==== $m" -ForegroundColor Cyan }
function Ok([string]$m) { Write-Host "  [ok] $m" -ForegroundColor Green }
function Note([string]$m) { Write-Host "  -  $m" }
function Warn([string]$m) { Write-Host "  [!] $m" -ForegroundColor Yellow }
function Bad([string]$m) { Write-Host "  [x] $m" -ForegroundColor Red }
function Die([string]$m) { throw $m }

function WriteNoBom([string]$Path, [string]$Text) {
  $enc = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($Path, $Text, $enc)
}

function Nat([string]$Exe, [object[]]$ArgList, [string]$LogPath = '') {
  if (-not $LogPath) { $LogPath = Join-Path $env:TEMP ('aqd-native-' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.log') }
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { & $Exe @ArgList *> $LogPath } catch { } finally { $ErrorActionPreference = $prev }
  $rc = $LASTEXITCODE
  return [pscustomobject]@{ rc = $rc; log = $LogPath }
}

function Ask([string]$Question, [string]$Default = '') {
  if ($Default) { $p = "$Question [$Default]" } else { $p = $Question }
  $v = Read-Host $p
  if (-not $v -and $Default) { return $Default }
  return $v
}

function AskSecret([string]$Question) {
  $redirected = $false
  try { $redirected = [Console]::IsInputRedirected } catch { $redirected = $false }
  if ($redirected) {
    Note "stdin is redirected (not a real console): reading $Question in clear text, no hidden input available"
    return Read-Host $Question
  }
  $sec = Read-Host -AsSecureString $Question
  $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
  try { return [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
  finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Confirm([string]$Question, [bool]$DefaultYes) {
  $hint = if ($DefaultYes) { '[Y/n]' } else { '[y/N]' }
  if ($Yes) {
    if ($DefaultYes) { Note "$Question $hint -> yes" } else { Note "$Question $hint -> no" }
    return $DefaultYes
  }
  $a = Read-Host "$Question $hint"
  if (-not $a) { return $DefaultYes }
  return [bool]($a -match '^(?i)y(es)?$')
}

function Find-Python {
  $cands = @()
  foreach ($n in @('python', 'python3')) {
    $c = Get-Command $n -ErrorAction SilentlyContinue
    if ($c -and $c.Source) { $cands += $c.Source }
  }
  if (Get-Command py -ErrorAction SilentlyContinue) {
    $r = Nat 'py' @('-3', '-c', 'import sys; print(sys.executable)')
    if ($r.rc -eq 0) { $cands += @((Get-Content $r.log -ErrorAction SilentlyContinue) | Where-Object { $_ } | Select-Object -First 1) }
  }
  foreach ($v in @('Python312', 'Python311', 'Python313', 'Python310')) {
    $cands += (Join-Path $env:LOCALAPPDATA "Programs\Python\$v\python.exe")
    $cands += "C:\$v\python.exe"
  }
  foreach ($p in $cands) {
    if (-not $p) { continue }
    $p = ([string]$p).Trim()
    if ($p -match '\\WindowsApps\\') { continue }
    if (-not (Test-Path $p)) { continue }
    $r = Nat $p @('-c', 'import sys; print(sys.version_info[0])')
    if ($r.rc -eq 0) { return $p }
  }
  return $null
}

function Get-AqdProcs([string]$ForRoot) {
  $all = @(Get-CimInstance Win32_Process -Filter "name like 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'alt\.py|safe_run\.py|menu_worker\.py' })
  if ($ForRoot -eq $DefaultRoot) { return $all }
  $needle = [regex]::Escape($ForRoot)
  return @($all | Where-Object { $_.CommandLine -match $needle })
}

function Get-Source([string]$Work, [string]$Ref) {
  if (Get-Command git -ErrorAction SilentlyContinue) {
    $dir = Join-Path $Work 'repo'
    Note "git clone --depth 1 --branch $Ref $RepoGit"
    $r = Nat 'git' @('clone', '--quiet', '--depth', '1', '--branch', $Ref, $RepoGit, $dir)
    if ($r.rc -eq 0 -and (Test-Path (Join-Path $dir 'common'))) { return $dir }
    Warn "git clone failed (rc=$($r.rc)); falling back to zip"
    Get-Content $r.log -Tail 5 -ErrorAction SilentlyContinue | ForEach-Object { Note $_ }
  } else { Note "no git; using zip" }
  $zip = Join-Path $Work 'src.zip'
  $url = if ($Ref -eq 'main') { $RepoZip } else { "https://codeload.github.com/truonglh2372/script_google_map/zip/refs/heads/$Ref" }
  Note "codeload zip: $url"
  Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip
  $un = Join-Path $Work 'unzip'
  Expand-Archive -Path $zip -DestinationPath $un -Force
  $dir = Get-ChildItem $un -Directory | Select-Object -First 1
  if (-not $dir) { Die "zip has no top folder" }
  return $dir.FullName
}

function Install-Flat([string]$Src, [string]$NewRoot, [string]$Py, [string]$Suffixes, [string]$Parity) {
  New-Item -ItemType Directory -Force -Path $NewRoot | Out-Null
  $copied = 0
  foreach ($f in Get-ChildItem (Join-Path $Src 'common') -File) {
    Copy-Item -Force $f.FullName (Join-Path $NewRoot $f.Name)
    $copied++
  }
  foreach ($f in @('alt.py', 'start_fetcher.ps1', 'start_menu_worker.ps1', 'aqd_fetcher.cmd', 'aqd_menu_worker.cmd')) {
    $from = Join-Path $Src "windows\$f"
    if (Test-Path $from) { Copy-Item -Force $from (Join-Path $NewRoot $f); $copied++ } else { Warn "missing windows\$f" }
  }
  Ok "copied $copied files into $NewRoot (FLAT layout: every .py sits directly in $NewRoot)"
  $envLine = "`$env:AQD_ROOT='$NewRoot'; `$env:AQD_ID_SUFFIXES='$Suffixes'; `$env:AQD_PARITY='$Parity'`r`n"
  foreach ($f in @('start_fetcher.ps1', 'start_menu_worker.ps1')) {
    $p = Join-Path $NewRoot $f
    if (-not (Test-Path $p)) { Warn "missing $f"; continue }
    $t = [System.IO.File]::ReadAllText($p)
    $t = [regex]::Replace($t, [regex]::Escape($DefaultRoot), { param($m) $NewRoot })
    $t = [regex]::Replace($t, "(?i)'[^'\r\n]*[\\/]python(?:w)?\.exe'", { param($m) "'$Py'" })
    WriteNoBom $p ($envLine + $t)
    Ok "patched $($f): python path + root + env"
  }
  foreach ($f in @('aqd_fetcher.cmd', 'aqd_menu_worker.cmd')) {
    $p = Join-Path $NewRoot $f
    if (-not (Test-Path $p)) { continue }
    $t = [System.IO.File]::ReadAllText($p)
    WriteNoBom $p ([regex]::Replace($t, [regex]::Escape($DefaultRoot), { param($m) $NewRoot }))
  }
}

function Start-Chrome([string]$NewRoot, [string]$Port) {
  $script:cdpUp = $false
  try { $script:cdpUp = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:$Port/json/version").StatusCode -eq 200 } catch { $script:cdpUp = $false }
  if ($script:cdpUp) { Ok "Chrome CDP $Port already open - reusing it, not opening another"; return }
  $exe = @(
    'C:\Program Files\Google\Chrome\Application\chrome.exe',
    'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
  ) | Where-Object { Test-Path $_ } | Select-Object -First 1
  if (-not $exe) { Warn "chrome.exe not found - open Chrome yourself with --remote-debugging-port=$Port"; return }
  Note "opening Chrome (CDP $Port, profile $NewRoot\chrome-profile)"
  Start-Process $exe -ArgumentList '--no-first-run', '--remote-debugging-address=127.0.0.1', "--remote-debugging-port=$Port", "--user-data-dir=$NewRoot\chrome-profile", '--restore-last-session'
  for ($i = 0; $i -lt 30 -and -not $script:cdpUp; $i++) {
    Start-Sleep 2
    try { $script:cdpUp = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:$Port/json/version").StatusCode -eq 200 } catch { $script:cdpUp = $false }
  }
  if ($script:cdpUp) { Ok "Chrome CDP $Port is up" } else { Warn "Chrome CDP $Port not up after 60s" }
}

function Backup-State([string]$NewRoot) {
  $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
  $dst = "C:\aqd_backup_$stamp"
  New-Item -ItemType Directory -Force -Path $dst | Out-Null
  $creds = Join-Path $NewRoot 'creds.env'
  if (Test-Path $creds) { Copy-Item -Force $creds (Join-Path $dst 'creds.env'); Ok "backed up creds.env" }
  foreach ($d in @('chrome-profile', 'aqd_data')) {
    $src = Join-Path $NewRoot $d
    if (-not (Test-Path $src)) { continue }
    $r = Nat 'robocopy' @($src, (Join-Path $dst $d), '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/R:1', '/W:1')
    if ($r.rc -lt 8) { Ok "backed up $d -> $(Join-Path $dst $d)" } else { Warn "robocopy $d rc=$($r.rc) (see $($r.log))" }
  }
  return $dst
}

function Show-Report([string]$NewRoot) {
  $data = Join-Path $NewRoot 'aqd_data'
  $dirs = 0; $done = 0; $prep = 0
  if (Test-Path $data) {
    $dirs = @(Get-ChildItem $data -Directory -ErrorAction SilentlyContinue).Count
    $done = @(Get-ChildItem $data -Recurse -Filter 'SAFE_DONE' -File -ErrorAction SilentlyContinue).Count
    $prep = @(Get-ChildItem $data -Recurse -Filter 'SAFE_PREP' -File -ErrorAction SilentlyContinue).Count
  }
  Note "aqd_data: $dirs places | SAFE_DONE: $done | SAFE_PREP: $prep"
  $lg = Join-Path $NewRoot 'safe_run_log.md'
  if (Test-Path $lg) { Note "safe_run_log.md: $((Get-Content $lg -ErrorAction SilentlyContinue | Measure-Object).Count) lines" } else { Note "safe_run_log.md: none" }
  if (Test-Path (Join-Path $NewRoot 'fetch_backoff.txt')) { Note "fetch_backoff.txt: PRESENT (backing off)" } else { Note "fetch_backoff.txt: none" }
  $cp = Join-Path $NewRoot 'chrome-profile'
  if (Test-Path $cp) {
    $bytes = (Get-ChildItem $cp -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    if (-not $bytes) { $bytes = 0 }
    if ($bytes -ge 1048576) { $sizeTxt = "{0:N1} MB" -f ($bytes / 1MB) } else { $sizeTxt = "{0:N0} KB" -f ($bytes / 1KB) }
    Note "chrome-profile: $sizeTxt (Google login session - losing it means logging in again)"
  } else { Note "chrome-profile: none" }
  if (Test-Path (Join-Path $NewRoot 'creds.env')) { Note "creds.env: present" } else { Note "creds.env: none" }
  Note "queue*.json: $(@(Get-ChildItem $NewRoot -Filter 'queue*.json' -File -ErrorAction SilentlyContinue).Count) file(s)"
}

function Stop-Old([string]$NewRoot) {
  New-Item -ItemType File -Force -Path (Join-Path $NewRoot 'STOP_ALL') | Out-Null
  New-Item -ItemType File -Force -Path (Join-Path $NewRoot 'STOP') | Out-Null
  Ok "created STOP_ALL + STOP - waiting up to 60s for alt.py/safe_run.py to stop at a place boundary"
  for ($i = 0; $i -lt 60; $i++) {
    if (@(Get-AqdProcs $NewRoot).Count -eq 0) { Ok "no aqd process left"; return }
    Start-Sleep 1
  }
  $left = @(Get-AqdProcs $NewRoot)
  if ($left.Count -eq 0) { Ok "exited cleanly"; return }
  Warn "$($left.Count) process(es) still running after 60s:"
  foreach ($x in $left) { Note "pid=$($x.ProcessId) $($x.CommandLine)" }
  if (Confirm "Kill -Force $($left.Count) process(es) above?" $false) {
    foreach ($x in $left) { Stop-Process -Id $x.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep 2
    Ok "killed; left: $(@(Get-AqdProcs $NewRoot).Count)"
  } else {
    Warn "old processes left alone - the reinstall may fight over files"
  }
}

function Clear-State([string]$NewRoot, [bool]$RemoveProfile) {
  foreach ($f in @('STOP_ALL', 'STOP', 'STOP_MENU', 'parity_tries.json', 'fetch_backoff.txt', 'alt.out',
      'safe_run_log.md', 'aqd_progress.md', 'aqd_api_log.md', 'start_fetcher.log', 'menu_worker.lock', 'photos_backoff.txt')) {
    $p = Join-Path $NewRoot $f
    if (Test-Path $p) { Remove-Item -Force -Recurse $p -ErrorAction SilentlyContinue; Note "deleted $f" }
  }
  foreach ($f in Get-ChildItem $NewRoot -Filter 'queue*.json' -File -ErrorAction SilentlyContinue) { Remove-Item -Force $f.FullName; Note "deleted $($f.Name)" }
  foreach ($d in @('__pycache__', '.aqd_session')) {
    $p = Join-Path $NewRoot $d
    if (Test-Path $p) { Remove-Item -Force -Recurse $p -ErrorAction SilentlyContinue; Note "deleted $d\" }
  }
  if ($RemoveProfile) {
    $p = Join-Path $NewRoot 'chrome-profile'
    if (Test-Path $p) { Remove-Item -Force -Recurse $p -ErrorAction SilentlyContinue; Warn "deleted chrome-profile\ - you MUST log in to Google again" }
  } else { Note "kept chrome-profile\" }
  Note "kept creds.env and aqd_data\"
}

function Write-Creds([string]$NewRoot) {
  $path = Join-Path $NewRoot 'creds.env'
  if (Test-Path $path) {
    Note "creds.env already exists"
    if (-not (Confirm "Overwrite creds.env with new values?" $false)) { Note "keeping the existing creds.env"; return }
  }
  Say ""
  Note "One prompt per environment variable; values go straight into $path (gitignored)."
  $email = Ask 'AQD_ADMIN_EMAIL'
  if (-not $email) { Die 'AQD_ADMIN_EMAIL must not be empty' }
  $pw = AskSecret 'AQD_ADMIN_PASSWORD (hidden while typing)'
  if (-not $pw) { Die 'AQD_ADMIN_PASSWORD must not be empty' }
  $base = Ask 'AQD_BASE' 'https://anuongquanhday.com'
  WriteNoBom $path "AQD_ADMIN_EMAIL=$email`r`nAQD_ADMIN_PASSWORD=$pw`r`nAQD_BASE=$base`r`n"
  Ok "wrote creds.env (AQD_ADMIN_EMAIL, AQD_ADMIN_PASSWORD, AQD_BASE - password never printed)"
}

function Count-CredKeys([string]$NewRoot) {
  $path = Join-Path $NewRoot 'creds.env'
  if (-not (Test-Path $path)) { return 0 }
  return @([System.IO.File]::ReadAllLines($path) | Where-Object { $_.Trim() -and -not $_.Trim().StartsWith('#') -and $_.Contains('=') }).Count
}

function Ask-Suffixes([string]$NewRoot) {
  Say ""
  Note "ID suffixes: keep only IDs whose LAST DIGIT is in the list (e.g. '1 3 5 7 9', or '2 4')."
  Note "Empty = fall back to the old AQD_PARITY (even/odd), compatible with the running Mac."
  while ($true) {
    $raw = Ask 'AQD_ID_SUFFIXES'
    $norm = ([string]$raw).Replace(',', ' ').Trim()
    if (-not $norm) {
      $par = ([string](Ask 'AQD_PARITY (even/odd)' 'even')).ToLower()
      if ($par -ne 'even' -and $par -ne 'odd') { Warn "AQD_PARITY must be even or odd"; continue }
      return @{ suffixes = ''; parity = $par }
    }
    $parts = $norm.Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries)
    $bad = @($parts | Where-Object { $_.Length -ne 1 -or $_ -notmatch '^[0-9]$' })
    if ($bad.Count -gt 0) { Warn "invalid: $($bad -join ', ') - only single digits 0-9 are accepted"; continue }
    return @{ suffixes = (@($parts | Sort-Object -Unique) -join ' '); parity = 'even' }
  }
}

function Invoke-Install {
  $clean = $Clean.IsPresent
  if (-not $clean -and $env:AQD_CLEAN) { $clean = [bool]($env:AQD_CLEAN -notmatch '^(?i)(0|false|no|off)$') }
  $newRoot = $Root
  if (-not $newRoot) { $newRoot = $env:AQD_ROOT }
  if (-not $newRoot) { $newRoot = $DefaultRoot }
  $newRoot = ([string]$newRoot).TrimEnd('\')
  $ref = $env:AQD_REPO_REF
  if (-not $ref) { $ref = 'main' }

  $work = Join-Path $env:TEMP ('aqd-install-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
  New-Item -ItemType Directory -Force -Path $work | Out-Null
  try {
    Head "aqd installer - one command"
    Note "root   : $newRoot"
    Note "mode   : $(if ($clean) { 'CLEAN reinstall' } else { 'fresh install / update' })"
    Note "ref    : $ref"
    Note "payload: $RepoGit"

    Head "1/8 Python"
    $py = Find-Python
    if (-not $py) { Die "no real Python found. Install it then re-run: winget install -e --id Python.Python.3.12 (and turn off the python.exe App execution alias)" }
    $ver = Nat $py @('-c', 'import sys; print(sys.version.split()[0])')
    $verTxt = if (Test-Path $ver.log) { ((Get-Content $ver.log -ErrorAction SilentlyContinue) -join ' ').Trim() } else { '?' }
    Ok "python: $py ($verTxt)"

    $existed = Test-Path $newRoot
    if (-not $existed) { New-Item -ItemType Directory -Force -Path $newRoot | Out-Null; Ok "created $newRoot" }

    if ($clean -and $existed) {
      Head "2/8 Stop old processes"
      Stop-Old $newRoot
      Head "3/8 Report before wiping"
      Show-Report $newRoot
      Head "4/8 Rescue backup"
      if (Confirm "Back up creds.env + chrome-profile + aqd_data to C:\aqd_backup_<ts>?" $true) {
        $backup = Backup-State $newRoot
        Ok "backup: $backup"
      } else {
        $backup = $null
        Warn "NO backup. Losing chrome-profile means you MUST log in to Google again."
      }
      Head "5/8 Wipe conflicting state"
      $rmProfile = Confirm "Also delete chrome-profile (Google login session)?" $false
      if ($rmProfile) { Warn "chrome-profile will be deleted - you must log in to Google again$(if ($backup) { " (copy kept in $backup)" })" }
      Clear-State $newRoot $rmProfile
    } else {
      Head "2/8 Stop old processes"
      Note $(if ($clean) { "skipped: $newRoot does not exist yet" } else { "skipped: update mode" })
      Head "3/8 Report before wiping"
      Note "skipped"
      Head "4/8 Rescue backup"
      Note "skipped"
      Head "5/8 Wipe conflicting state"
      Note "skipped: update source only, keep creds.env / chrome-profile / aqd_data"
    }

    Head "6/8 Fetch source + install flat"
    $src = Get-Source $work $ref
    Ok "source: $src"
    $cfg = Ask-Suffixes $newRoot
    WriteNoBom (Join-Path $newRoot 'aqd_config.json') (@{ id_suffixes = $cfg.suffixes; parity = $cfg.parity } | ConvertTo-Json)
    if ($cfg.suffixes) { Ok "aqd_config.json: AQD_ID_SUFFIXES='$($cfg.suffixes)'" } else { Ok "aqd_config.json: AQD_PARITY='$($cfg.parity)' (no suffixes)" }
    Install-Flat $src $newRoot $py $cfg.suffixes $cfg.parity
    Write-Creds $newRoot
    if ((Count-CredKeys $newRoot) -lt 2) { Warn "creds.env is missing keys - alt.py will fail when it starts" }

    Head "7/8 Install dependencies"
    Ok "required: $($RequiredPkgs -join ', ')"
    $r = Nat $py (@('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input') + $RequiredPkgs)
    if ($r.rc -eq 0) { Ok "required packages installed" } else { Warn "pip rc=$($r.rc) - see $($r.log)"; Get-Content $r.log -Tail 12 -ErrorAction SilentlyContinue | ForEach-Object { Note $_ } }
    $heavy = -not $SkipHeavy.IsPresent
    if ($heavy) { $heavy = Confirm "Install the HEAVY set (open_clip_torch + torch + easyocr, ~250MB+)? Better CLIP/OCR image filtering." $true }
    if ($heavy) {
      Ok "heavy set: $($HeavyPkgs -join ', ')"
      $r2 = Nat $py (@('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input') + $HeavyPkgs)
      if ($r2.rc -eq 0) { Ok "heavy packages installed" } else { Warn "pip rc=$($r2.rc) - still runs, only CLIP/OCR filtering is weaker" }
    } else { Note "heavy set skipped" }

    Head "8/8 Chrome + start"
    if ($NoChrome.IsPresent) { Warn "Chrome skipped (-NoChrome)" }
    else {
      Start-Chrome $newRoot '9222'
      Warn "REMEMBER: log in to Google in that Chrome window (the script cannot do it)"
    }
    $env:AQD_ROOT = $newRoot
    $env:AQD_ID_SUFFIXES = $cfg.suffixes
    $env:AQD_PARITY = $cfg.parity
    if (Confirm "Register auto-start at Windows login (Startup folder)?" ($newRoot -eq $DefaultRoot)) {
      $startup = [Environment]::GetFolderPath('Startup')
      foreach ($f in @('aqd_fetcher.cmd', 'aqd_menu_worker.cmd')) {
        $p = Join-Path $newRoot $f
        if (Test-Path $p) { Copy-Item -Force $p (Join-Path $startup $f); Ok "Startup\$f" }
      }
    } else { Note "no Startup entry" }

    if ($NoStart.IsPresent) { Warn "alt.py not started (-NoStart)" }
    else {
      if (@(Get-AqdProcs $newRoot).Count -gt 0) { Warn "alt.py/safe_run.py for $newRoot already running - not starting another" }
      else {
        Start-Process -WindowStyle Hidden -FilePath $py -ArgumentList (Join-Path $newRoot 'alt.py') -WorkingDirectory $newRoot
        Ok "started alt.py (log: $newRoot\alt.out)"
      }
    }

    Say ""
    Say "================ DONE ================" -ForegroundColor Green
    Note "root   : $newRoot"
    Note "python : $py"
    Note "id pick: $(if ($cfg.suffixes) { "suffixes $($cfg.suffixes)" } else { "parity=$($cfg.parity)" })"
    Note "creds  : $(Join-Path $newRoot 'creds.env')"
    Note "config : $(Join-Path $newRoot 'aqd_config.json')"
    Note "logs   : $newRoot\alt.out , $newRoot\safe_run_log.md"
    Note "stop   : create file $newRoot\STOP_ALL"
    Say "=====================================" -ForegroundColor Green
  } finally {
    Remove-Item -Force -Recurse $work -ErrorAction SilentlyContinue
  }
}

try { Invoke-Install }
catch {
  Say ""
  Bad "INSTALL STOPPED: $($_.Exception.Message)"
  Note "Fix the cause and paste the same single command again - the installer is re-runnable."
}
