# Offline guard tests for windows/install.ps1: the root-scoped process guard, the Chrome profile/root check,
# the suffix vs parity overlap warning, CLEAN removing stray .py while keeping creds.env / chrome-profile /
# aqd_data / aqd_config.json, and "every prompt has a safe default (Enter works)".
# No network, no package download, no C:\aqd touched. ASCII only on purpose (Windows PowerShell 5.1).
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here
$target = Join-Path $repo 'windows\install.ps1'
$script:failures = @()
$script:inputs = New-Object System.Collections.Queue

function Say([string]$m) { Write-Host $m }
function Head([string]$m) { Write-Host ""; Write-Host "==== $m" }
function Ok([string]$m) { Write-Host "  [ok] $m" }
function Note([string]$m) { Write-Host "  -  $m" }
function Warn([string]$m) { Write-Host "  [!] $m" }
function Bad([string]$m) { Write-Host "  [x] $m" }

function Check([string]$label, $got, $want) {
  if ("$got" -eq "$want") { Write-Host "  ok   ${label}: $got" }
  else { Write-Host "  FAIL ${label}: got [$got] want [$want]"; $script:failures += $label }
}

function CheckTrue([string]$label, [bool]$cond, [string]$detail = '') {
  if ($cond) { Write-Host ("  ok   $label" + $(if ($detail) { " ($detail)" })) }
  else { Write-Host "  FAIL $label $detail"; $script:failures += $label }
}

# The installer is patched to any root; here the prompt funnel runs as if stdin were redirected, so every
# Read-Host call returns the next scripted line (or "" = plain Enter).
function Test-StdinRedirected { return $true }
function Read-Host {
  param([switch]$AsSecureString, [string]$Prompt, [switch]$MaskInput, [Parameter(ValueFromRemainingArguments = $true)]$Rest)
  if ($script:inputs.Count -gt 0) { return $script:inputs.Dequeue() }
  return ''
}
function Set-Inputs([object[]]$lines) { $script:inputs = New-Object System.Collections.Queue; foreach ($l in $lines) { $script:inputs.Enqueue($l) } }

$Yes = $false
$DefaultBase = 'https://anuongquanhday.com'
$DefaultListPages = '20'
$DefaultFetchLimit = '0'

$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($target, [ref]$tokens, [ref]$errors)
$raw = Get-Content $target -Raw
CheckTrue "install.ps1 parses without errors" ($errors.Count -eq 0) ("$($errors.Count) error(s)")

$want = @('Normalize-Path', 'Get-RootNeedle', 'Test-IsAqdCmd', 'Select-AqdProcsInRoot', 'Select-AqdProcsOtherRoot',
          'Decide-Chrome', 'Get-ParityOverlapWarnings', 'Get-PayloadPyNames', 'Clear-StalePy',
          'Read-CredValue', 'Read-Line', 'Ask', 'AskSecret', 'Confirm',
          'Ask-Suffixes', 'Ask-ListOptions', 'Ask-FetchLimit')
$defs = @($ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $want -contains $n.Name }, $true))
Check "extracted functions" (($defs | ForEach-Object { $_.Name } | Sort-Object) -join ',') (($want | Sort-Object) -join ',')
foreach ($d in $defs) { Invoke-Expression $d.Extent.Text }

Head "(i) process guard is root-scoped - a foreign root is never selected"
$procs = @(
  [pscustomobject]@{ ProcessId = 40708; CommandLine = 'C:\Python312\python.exe C:\aqd\alt.py' },
  [pscustomobject]@{ ProcessId = 12252; CommandLine = 'C:\Python312\python.exe C:/aqd/safe_run.py' },
  [pscustomobject]@{ ProcessId = 90001; CommandLine = 'C:\Python312\python.exe C:\aqd_fix\alt.py' },
  [pscustomobject]@{ ProcessId = 90002; CommandLine = 'C:\Python312\python.exe C:\aqd_fix\safe_run.py' },
  [pscustomobject]@{ ProcessId = 90003; CommandLine = 'C:\Python312\python.exe C:\aqd_fix2\menu_worker.py' }
)
Check "in root C:\aqd" ((@(Select-AqdProcsInRoot $procs 'C:\aqd') | ForEach-Object { $_.ProcessId } | Sort-Object) -join ',') '12252,40708'
Check "in root C:\aqd_fix" ((@(Select-AqdProcsInRoot $procs 'C:\aqd_fix') | ForEach-Object { $_.ProcessId } | Sort-Object) -join ',') '90001,90002'
Check "not in root C:\aqd_fix (the ones to warn about, never kill)" ((@(Select-AqdProcsOtherRoot $procs 'C:\aqd_fix') | ForEach-Object { $_.ProcessId } | Sort-Object) -join ',') '12252,40708,90003'
Check "C:\aqd does not swallow C:\aqd_fix (the old bug)" (($procs | Where-Object { (Normalize-Path $_.CommandLine) -match [regex]::Escape((Get-RootNeedle 'C:\aqd')) } | ForEach-Object { $_.ProcessId } | Sort-Object) -join ',') '12252,40708'
CheckTrue "a non-aqd python process is ignored" ((@(Select-AqdProcsInRoot @([pscustomobject]@{ ProcessId = 1; CommandLine = 'python -m http.server' }) 'C:\aqd')).Count -eq 0)
CheckTrue "the Stop-Old prompt names the root and says only those" (($raw -match 'Kill -Force the \$\(') -and ($raw -match '\(and only those\)'))
CheckTrue "other-root processes are only warned about, never killed" ($raw -match 'will NOT be touched')

Head "(ii) Chrome on a foreign root's profile is NOT reused"
Check "port down -> free" (Decide-Chrome $false '' 'C:\aqd\chrome-profile') 'free'
Check "own profile -> reuse" (Decide-Chrome $true 'C:\aqd\chrome-profile' 'C:\aqd\chrome-profile') 'reuse'
Check "own profile, different case/sep -> reuse" (Decide-Chrome $true 'C:/AQD/chrome-profile' 'C:\aqd\chrome-profile') 'reuse'
Check "other root profile -> foreign" (Decide-Chrome $true 'C:\aqd_fix\chrome-profile' 'C:\aqd\chrome-profile') 'foreign'
Check "no owner found but port up -> foreign (do not reuse)" (Decide-Chrome $true '' 'C:\aqd\chrome-profile') 'foreign'
CheckTrue "Chrome owner is read from --user-data-dir on the CDP port" ($raw -match 'Get-ChromeUserDataDir')
CheckTrue "a foreign port makes the installer move to the next port" ($raw -match "not reusing it, trying the next port")
CheckTrue "start_fetcher reads --user-data-dir too" ((Get-Content (Join-Path $repo 'windows\start_fetcher.ps1') -Raw) -match 'user-data-dir')
CheckTrue "start_fetcher mutex is per root" ((Get-Content (Join-Path $repo 'windows\start_fetcher.ps1') -Raw) -match 'aqd_fetcher_launcher_\$tag')

Head "(iii) suffix vs parity overlap warning"
$oddWarn = @(Get-ParityOverlapWarnings '9')
Check "single odd suffix" ($oddWarn.Count -eq 1) 'True'
Check "odd warning text" $oddWarn[0] 'suffix 9 is INSIDE parity=odd (1,3,5,7,9) - if another machine runs parity=odd you will both fetch id%10=9. Set the other machine to a disjoint set.'
$evenWarn = @(Get-ParityOverlapWarnings '2 4')
Check "even set warning text" $evenWarn[0] 'suffixes 2,4 are INSIDE parity=even (0,2,4,6,8) - if another machine runs parity=even you will both fetch id%10=2,4. Set the other machine to a disjoint set.'
Check "a mixed set overlaps both parities" (@(Get-ParityOverlapWarnings '1 2').Count) '2'
Check "the classic odd set (== parity odd)" (@(Get-ParityOverlapWarnings '1 3 5 7 9').Count) '1'
Check "empty suffixes -> no warning" (@(Get-ParityOverlapWarnings '').Count) '0'
CheckTrue "installer prints the warning" ($raw -match 'Get-ParityOverlapWarnings')

Head "(iv) CLEAN removes stray .py, keeps creds.env / chrome-profile / aqd_data / aqd_config.json"
$tmp = Join-Path $env:TEMP ('aqd-guard-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
  foreach ($f in @('safe_run.py', 'alt.py', 'legacy_extra.py', 'old_module.py', 'creds.env', 'aqd_config.json')) {
    Set-Content -Path (Join-Path $tmp $f) -Value 'x' -Encoding ascii
  }
  New-Item -ItemType Directory -Force -Path (Join-Path $tmp '__pycache__') | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $tmp 'chrome-profile') | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $tmp 'aqd_data') | Out-Null
  $bak = Join-Path $tmp 'bak'
  $removed = @(Clear-StalePy $tmp @('safe_run.py', 'alt.py') $bak)
  Check "removed the two stray .py" (($removed | Sort-Object) -join ',') 'legacy_extra.py,old_module.py'
  CheckTrue "stray legacy_extra.py is gone" (-not (Test-Path (Join-Path $tmp 'legacy_extra.py')))
  CheckTrue "stray old_module.py is gone" (-not (Test-Path (Join-Path $tmp 'old_module.py')))
  CheckTrue "kept payload safe_run.py" (Test-Path (Join-Path $tmp 'safe_run.py'))
  CheckTrue "kept payload alt.py" (Test-Path (Join-Path $tmp 'alt.py'))
  CheckTrue "__pycache__ is gone" (-not (Test-Path (Join-Path $tmp '__pycache__')))
  CheckTrue "kept creds.env" (Test-Path (Join-Path $tmp 'creds.env'))
  CheckTrue "kept aqd_config.json" (Test-Path (Join-Path $tmp 'aqd_config.json'))
  CheckTrue "kept chrome-profile\" (Test-Path (Join-Path $tmp 'chrome-profile'))
  CheckTrue "kept aqd_data\" (Test-Path (Join-Path $tmp 'aqd_data'))
  CheckTrue "backed up the stray .py first" ((Test-Path (Join-Path $bak 'legacy_extra.py')) -and (Test-Path (Join-Path $bak 'old_module.py')))
  $removed2 = @(Clear-StalePy $tmp @() $bak)
  Check "no payload list -> refuses to delete" $removed2.Count '0'
  CheckTrue "refusal kept the files" ((Test-Path (Join-Path $tmp 'safe_run.py')) -and (Test-Path (Join-Path $tmp 'creds.env')))
  $payload = @(Get-PayloadPyNames $repo)
  CheckTrue "payload list comes from the fetched source" (($payload -contains 'safe_run.py') -and ($payload -contains 'alt.py') -and ($payload -contains 'photos_new.py'))
} finally { Remove-Item -Force -Recurse $tmp -ErrorAction SilentlyContinue }

Head "(v) every prompt has a safe default (Enter works)"
Set-Inputs @('')
Check "Ask always returns the default on Enter" (Ask 'AQD_BASE' 'https://anuongquanhday.com') 'https://anuongquanhday.com'
Set-Inputs @('')
Check "list pages default" (Ask-ListOptions) '20'
Set-Inputs @('0')
Check "list pages accepts 0" (Ask-ListOptions) '0'
Set-Inputs @('')
Check "fetch limit default" (Ask-FetchLimit) '0'
Set-Inputs @('')
Check "Confirm default no" (Confirm 'Install CLIP?' $false) 'False'
Set-Inputs @('')
Check "Confirm default yes" (Confirm 'Back up?' $true) 'True'
Set-Inputs @('y')
Check "Confirm yes" (Confirm 'Back up?' $false) 'True'
Set-Inputs @('n')
Check "Confirm no" (Confirm 'Back up?' $true) 'False'
Set-Inputs @('')
Check "AskSecret Enter keeps the existing creds value" (AskSecret 'pw' 'existing-secret') 'existing-secret'
Set-Inputs @('', 'abc', '10', '9')
Check "Ask-Suffixes re-asks on empty/invalid and takes a valid set" ((Ask-Suffixes).suffixes) '9'
Set-Inputs @('even')
Check "Ask-Suffixes accepts 'even'" ((Ask-Suffixes).parity) 'even'
Set-Inputs @('odd')
Check "Ask-Suffixes accepts 'odd'" ((Ask-Suffixes).parity) 'odd'
Set-Inputs @('3,1,3')
Check "Ask-Suffixes de-dupes and sorts" ((Ask-Suffixes).suffixes) '1 3'

$askCalls = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Ask' }, $true))
$badAsk = @($askCalls | Where-Object {
    $els = $_.CommandElements
    $hasDefault = ($els.Count -ge 3)
    $isReq = [bool]($els | Where-Object { $_ -is [System.Management.Automation.Language.CommandParameterAst] -and $_.ParameterName -eq 'Required' })
    -not ($hasDefault -or $isReq) })
CheckTrue "every Ask has a default or -Required ($($askCalls.Count) calls)" ($badAsk.Count -eq 0) (($badAsk | ForEach-Object { $_.Extent.Text }) -join ' | ')

$confirmCalls = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Confirm' }, $true))
$badConfirm = @($confirmCalls | Where-Object { $_.CommandElements.Count -lt 3 })
CheckTrue "every Confirm has a default ($($confirmCalls.Count) calls)" ($badConfirm.Count -eq 0) (($badConfirm | ForEach-Object { $_.Extent.Text }) -join ' | ')

$secretCalls = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'AskSecret' }, $true))
$badSecret = @($secretCalls | Where-Object { $_.CommandElements.Count -lt 3 })
CheckTrue "every AskSecret passes a default ($($secretCalls.Count) call)" ($badSecret.Count -eq 0) (($badSecret | ForEach-Object { $_.Extent.Text }) -join ' | ')

CheckTrue "no raw Read-Host outside the two funnel functions" (
  (@([regex]::Matches($raw, 'Read-Host')).Count -le 4) -and ($raw -match 'Test-StdinRedirected') -and ($raw -match 'the value you type WILL be shown here in clear text'))
CheckTrue "redirected stdin prints the prompt first" ($raw -match 'if \(Test-StdinRedirected\) \{ Say \$Prompt; return \(Read-Host\) \}')
CheckTrue "the default summary line is printed" ($raw -match 'Enter = defaults:')
CheckTrue "CLIP/OCR default to no (Enter = skip)" ($raw -match 'MB, 5-20 min\. \$\(') -and ($raw -match '\$false\)\) \{ Note "skipped')

$credTmp = Join-Path $env:TEMP ('aqd-cred-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $credTmp | Out-Null
try {
  Set-Content -Path (Join-Path $credTmp 'creds.env') -Value "AQD_ADMIN_EMAIL=a@b.c`r`nAQD_ADMIN_PASSWORD=`"p@ss=word`"`r`nAQD_BASE=x`r`n" -Encoding ascii
  Check "Read-CredValue finds the password" (Read-CredValue $credTmp 'AQD_ADMIN_PASSWORD') 'p@ss=word'
  Check "Read-CredValue finds the email" (Read-CredValue $credTmp 'AQD_ADMIN_EMAIL') 'a@b.c'
  Check "Read-CredValue missing key -> empty" (Read-CredValue $credTmp 'NOPE') ''
} finally { Remove-Item -Force -Recurse $credTmp -ErrorAction SilentlyContinue }

if ($script:failures.Count) {
  Write-Host ""
  Write-Host "$($script:failures.Count) FAILED: $($script:failures -join ', ')"
  exit 1
}
Write-Host ""
Write-Host "ALL OK"
exit 0
