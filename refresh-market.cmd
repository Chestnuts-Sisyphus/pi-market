@echo off
rem Refresh the published catalog by triggering the GitHub Action (recommended).
rem Local packaging is not needed: CI indexes pi.dev, packages new dependency-free
rem packages into a new immutable release, verifies hashes, and commits catalog.json.
setlocal
gh workflow run refresh.yml --repo Chestnuts-Sisyphus/pi-market
if errorlevel 1 (
  echo.
  echo Trigger failed. Check: gh auth status
  exit /b 1
)
echo.
echo Triggered. Watch progress:
echo   gh run watch --repo Chestnuts-Sisyphus/pi-market
echo Then restart PI-Desktop (or press "Refresh marketplace") to pick up the new catalog.
endlocal
