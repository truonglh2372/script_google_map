# Start the menu OCR worker for THIS root only (root-scoped single-instance check, ASCII only).
$root = if ($env:AQD_ROOT) { $env:AQD_ROOT } else { 'C:\aqd' }
$py = 'C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe'
function N($p){ if(-not $p){ return '' }; ([string]$p).Replace('\','/').TrimEnd('/').ToLowerInvariant() }
$needle = (N $root) + '/'
$here = Get-CimInstance Win32_Process -Filter "name like 'python%'" -ErrorAction SilentlyContinue | ?{ $_.CommandLine -match 'menu_worker\.py' -and (N $_.CommandLine) -like ("*" + $needle + "*") }
if(-not $here){ Start-Process -WindowStyle Hidden -FilePath $py -ArgumentList (Join-Path $root 'menu_worker.py') -WorkingDirectory $root }
