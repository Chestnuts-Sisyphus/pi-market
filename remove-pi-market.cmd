@echo off
rem Roll back to the Official marketplace channel.
rem Removes the persistent override and (optionally) closes a running instance so the
rem next launch reads the restored state. Installed plugins are NOT touched.
setlocal
reg delete "HKCU\Environment" /v PI_DESKTOP_PLUGIN_MARKET_URL /f >nul 2>&1
echo Removed PI_DESKTOP_PLUGIN_MARKET_URL (restores the Official channel on next launch).
echo Restart PI-Desktop for the change to take effect.
endlocal
