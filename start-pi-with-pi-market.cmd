@echo off
rem Start PI-Desktop with the pi-market catalog for THIS run only.
rem (The catalog is also set permanently for the user via:
rem     setx PI_DESKTOP_PLUGIN_MARKET_URL https://raw.githubusercontent.com/Chestnuts-Sisyphus/pi-market/main/catalog.json
rem  so a normal Start-menu launch works too once Explorer has picked the value up.)
setlocal
set "PI_DESKTOP_PLUGIN_MARKET_URL=https://raw.githubusercontent.com/Chestnuts-Sisyphus/pi-market/main/catalog.json"
start "" "D:\AI\PI\PI-Desktop\PI-Desktop.exe"
endlocal
