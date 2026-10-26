@echo off
rem Serve the adapter catalog over loopback (for the Custom catalog channel).
rem Keep this window open while PI-Desktop browses the marketplace.
setlocal
cd /d D:\AI\PI\pi-market
python pimarket.py serve --port 8765
endlocal
