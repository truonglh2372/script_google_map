# Auto-start the Admin fetcher for THIS root: a dedicated Chrome (its own CDP port + <root>\chrome-profile)
# then alt.py. Root-scoped: the single-instance mutex, the STOP_ALL check and the python process scan only
# look at this root; a Chrome already on the CDP port is reused only when its --user-data-dir is THIS root's
# profile, otherwise a free port is used (a new root never writes into an old root's profile).
# ASCII only on purpose (the installer prepends an env line; Windows PowerShell 5.1 reads a .ps1 without a BOM as ANSI).
$root = if ($env:AQD_ROOT) { $env:AQD_ROOT } else { 'C:\aqd' }
$py = 'C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe'
$chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
$log = Join-Path $root 'start_fetcher.log'
function L($m){ Add-Content $log ("{0} {1}" -f (Get-Date -f 'yyyy-MM-dd HH:mm:ss'), $m) }
function N($p){ if(-not $p){ return '' }; ([string]$p).Replace('\','/').TrimEnd('/').ToLowerInvariant() }
$needle = (N $root) + '/'
function AqdProcsHere { Get-CimInstance Win32_Process -Filter "name like 'python%'" -ErrorAction SilentlyContinue | ?{ $_.CommandLine -match 'alt\.py|safe_run\.py' -and (N $_.CommandLine) -like ("*" + $needle + "*") } }
function AqdProcsElsewhere { Get-CimInstance Win32_Process -Filter "name like 'python%'" -ErrorAction SilentlyContinue | ?{ $_.CommandLine -match 'alt\.py|safe_run\.py' -and (N $_.CommandLine) -notlike ("*" + $needle + "*") } }
function CdpUp([int]$p){ try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$p/json/version").StatusCode -eq 200 } catch { $false } }
function ChromeProfile([int]$p){
  $mdp = "--remote-debugging-port=$p"
  foreach($x in @(Get-CimInstance Win32_Process -Filter "name = 'chrome.exe'" -ErrorAction SilentlyContinue | ?{ $_.CommandLine -and ($_.CommandLine -match [regex]::Escape($mdp)) })){
    $m = [regex]::Match($x.CommandLine, '--user-data-dir=(?:"([^"]+)"|([^\s]+))')
    if($m.Success){ return ($m.Groups[1].Value + $m.Groups[2].Value) }
  }
  return ''
}
$tag = ($root -replace '[^A-Za-z0-9]','_')
$mtx = New-Object System.Threading.Mutex($false, "Global\aqd_fetcher_launcher_$tag")
if(-not $mtx.WaitOne(0)){ L "another launcher for $root is running; exit"; exit }
try {
  if(Test-Path (Join-Path $root 'STOP_ALL')){ L "STOP_ALL present in $root; not starting"; exit }
  if(AqdProcsHere){ L "alt/safe_run for $root already running; nothing to do"; exit }
  $other = @(AqdProcsElsewhere)
  if($other.Count -gt 0){ L ("$($other.Count) aqd process(es) belong to ANOTHER root - not touching them: " + (($other | %{ $_.ProcessId }) -join ',')) }
  $port = 9222
  $cfg = Join-Path $root 'aqd_config.json'
  if(Test-Path $cfg){ try { $c = Get-Content $cfg -Raw | ConvertFrom-Json; if($c.cdp_port){ $port = [int]$c.cdp_port } } catch { L "could not read $cfg : $_" } }
  if($env:AQD_CDP_PORT){ $port = [int]$env:AQD_CDP_PORT }
  $myProfile = Join-Path $root 'chrome-profile'
  $chosen = $null
  for($p = $port; $p -lt ($port + 50); $p++){
    if(-not (CdpUp $p)){ $chosen = $p; break }
    $owner = ChromeProfile $p
    if((N $owner) -eq (N $myProfile)){ $chosen = $p; L "CDP $p already open with this root's profile; reusing"; break }
    L "CDP $p is open with profile '$owner', not $myProfile - not reusing it"
  }
  if(-not $chosen){ L "no free CDP port near $port; exit"; exit }
  if(-not (CdpUp $chosen)){
    L "starting Chrome (CDP $chosen, profile $myProfile)"
    Start-Process $chrome -ArgumentList '--no-first-run','--remote-debugging-address=127.0.0.1',"--remote-debugging-port=$chosen","--user-data-dir=$myProfile",'--restore-last-session'
    for($i=0;$i -lt 30;$i++){ Start-Sleep 2; if(CdpUp $chosen){ break } }
    if(-not (CdpUp $chosen)){ L "Chrome CDP $chosen not up after 60s; abort"; exit }
    Start-Sleep 10
  }
  if(AqdProcsHere){ L 'alt appeared meanwhile; exit'; exit }
  $env:AQD_CDP_PORT = "$chosen"
  Start-Process -WindowStyle Hidden -FilePath $py -ArgumentList (Join-Path $root 'alt.py') -WorkingDirectory $root
  L "started alt.py (CDP $chosen)"
} finally { $mtx.ReleaseMutex() }
