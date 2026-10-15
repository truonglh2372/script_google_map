# Offline check for the installer's streamed native runner (Nat -Stream) and the pip/list options in
# windows/install.ps1. It never downloads a package: the "slow child" is a local python script.
# ASCII only on purpose: Windows PowerShell 5.1 reads a .ps1 without a BOM as the ANSI code page.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path (Split-Path -Parent $here) 'windows\install.ps1'
$script:failures = @()

function Say([string]$m) { Write-Host $m }
function Head([string]$m) { Write-Host ""; Write-Host "==== $m" }
function Ok([string]$m) { Write-Host "  [ok] $m" }
function Note([string]$m) { Write-Host "  -  $m" }
function Warn([string]$m) { Write-Host "  [!] $m" }
function Bad([string]$m) { Write-Host "  [x] $m" }

function Check([string]$label, $got, $want) {
  if ("$got" -eq "$want") { Write-Host "  ok   ${label}: $got" }
  else { Write-Host "  FAIL ${label}: got $got want $want"; $script:failures += $label }
}

function CheckTrue([string]$label, [bool]$cond, [string]$detail = '') {
  if ($cond) { Write-Host ("  ok   $label" + $(if ($detail) { " ($detail)" })) }
  else { Write-Host "  FAIL $label $detail"; $script:failures += $label }
}

$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($target, [ref]$tokens, [ref]$errors)
$raw = Get-Content $target -Raw
CheckTrue "install.ps1 parses without errors" ($errors.Count -eq 0) ("$($errors.Count) error(s)")

$want = @('Fmt-Secs', 'Show-LogTail', 'Nat', 'Python-Version')
$defs = @($ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $want -contains $n.Name }, $true))
Check "extracted functions" (($defs | ForEach-Object { $_.Name } | Sort-Object) -join ',') (($want | Sort-Object) -join ',')
foreach ($d in $defs) { Invoke-Expression $d.Extent.Text }

$tmp = Join-Path $env:TEMP ('aqd-stream-test-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
  $fake = @'
import time
print("Collecting torch", flush=True)
print("  Downloading torch-2.14.1-cp312-cp312-win_amd64.whl (118.4 MB)", flush=True)
for i in range(8):
    time.sleep(1)
    print("   %d0%% of 118.4 MB" % (i + 1), flush=True)
print("Installing collected packages: torch", flush=True)
'@
  $fakePath = Join-Path $tmp 'slow_pip.py'
  Set-Content -Path $fakePath -Value $fake -Encoding ascii
  $py = (Get-Command python).Source
  CheckTrue "python found for the fake child" ([bool]$py) $py

  Head "Python-Version probe"
  CheckTrue "the -c payload has no inner double quotes (PS 5.1 strips them)" `
    ($raw -match [regex]::Escape("-c', 'import sys; v=sys.version_info; print(v[0], v[1], v[2])'"))
  $pv = Python-Version $py
  CheckTrue "the probe returns a version object" ($null -ne $pv) "$pv"
  if ($pv) {
    Check "major is 3" $pv.major 3
    CheckTrue "text looks like a version" ($pv.text -match '^\d+\.\d+\.\d+$') $pv.text
  }

  Head "Nat -Stream: live output + heartbeat (child prints for 8s, Beat 4s)"
  $log = Join-Path $tmp 'slow.log'
  $trans = Join-Path $tmp 'transcript.txt'
  Start-Transcript -Path $trans -Force | Out-Null
  $r = Nat $py @($fakePath) $log -Stream -Beat 4 -Label 'fake pip (torch + open_clip_torch)'
  Stop-Transcript | Out-Null
  $txt = (Get-Content $trans -Raw) -replace "`r", ''
  Check "streamed rc" $r.rc 0
  CheckTrue "the child's first line was shown" ($txt -match [regex]::Escape('Collecting torch'))
  CheckTrue "the download line was shown" ($txt -match [regex]::Escape('Downloading torch-2.14.1'))
  CheckTrue "the last line was shown" ($txt -match [regex]::Escape('Installing collected packages: torch'))
  CheckTrue "a live heartbeat was printed" ($txt -match 'still running after')
  CheckTrue "the live log path is announced before the wait" ($txt -match [regex]::Escape('- live log:'))
  CheckTrue "the log file holds the whole output" ((Get-Content $log -Raw) -match [regex]::Escape('Installing collected packages'))
  CheckTrue "elapsed seconds are reported" ($r.secs -ge 7) ("secs=$($r.secs)")

  Head "Nat -KillAfterSec: a stuck child is stopped and reported"
  $r2 = Nat $py @($fakePath) (Join-Path $tmp 'kill.log') -Stream -Beat 60 -KillAfterSec 3 -Label 'fake pip'
  CheckTrue "the child was killed" ($r2.killed -eq $true)
  Check "killed rc is 124" $r2.rc 124
  CheckTrue "the kill is explained" ($r2.secs -ge 2) ("secs=$($r2.secs)")

  Head "Nat without -Stream: quiet and logged (the other callers are unchanged)"
  $r3 = Nat $py @('-c', 'print(40+2)') (Join-Path $tmp 'quiet.log')
  Check "quiet rc" $r3.rc 0
  Check "quiet log content" ((Get-Content (Join-Path $tmp 'quiet.log') -Raw).Trim()) '42'

  Head "install.ps1 wiring"
  CheckTrue "pip gets --timeout 60 --retries 5" ($raw -match "'--timeout', '60', '--retries', '5'")
  CheckTrue "the heavy groups stream" ($raw -match '-Stream -Label "pip ')
  CheckTrue "the heavy log is in the install root" ($raw -match "'pip-heavy\.log'")
  CheckTrue "CLIP and OCR are separate groups" (($raw -match "CLIP \(torch \+ open_clip_torch\)") -and ($raw -match "OCR \(easyocr\)"))
  CheckTrue "the torch wheel probe exists" ($raw -match "'index', 'versions', 'torch'")
  CheckTrue "Python 3.12 is preferred and the minor is checked" (($raw -match 'ver\.minor -ne 12') -and ($raw -match "12 \{ 0 \} 11 \{ 1 \}"))
  CheckTrue "list pages + fetch limit are asked" (($raw -match 'function Ask-ListOptions') -and ($raw -match 'function Ask-FetchLimit'))
  CheckTrue "both env vars reach the launcher" (($raw -match "AQD_LIST_MAX_PAGES='") -and ($raw -match "AQD_FETCH_LIMIT='"))
  CheckTrue "the DONE block explains the first listing" ($raw -match 'first start: lists')
  CheckTrue "ASCII only (no non-ASCII byte, no BOM)" (([System.IO.File]::ReadAllBytes($target) | Where-Object { $_ -gt 127 }).Count -eq 0)
} finally {
  Remove-Item -Force -Recurse $tmp -ErrorAction SilentlyContinue
}

if ($script:failures.Count) {
  Write-Host ""
  Write-Host "$($script:failures.Count) FAILED: $($script:failures -join ', ')"
  exit 1
}
Write-Host ""
Write-Host "ALL OK"
exit 0
