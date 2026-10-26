@echo off
rem Stop the local adapter catalog server (port 8765).
setlocal
powershell -NoProfile -Command "$p = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if ($p) { Stop-Process -Id $p.OwningProcess -Force; 'stopped pid ' + $p.OwningProcess } else { 'nothing listening on 8765' }"
endlocal
