@echo off
setlocal
if not exist config.json copy config.example.json config.json >nul
python demo_trader.py --config config.json --execute-demo --loop
pause
