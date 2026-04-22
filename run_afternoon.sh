#!/bin/bash
# Afternoon session runner — called by launchd at 14:25 Mon-Fri
cd /Users/_pporpin_/Documents/Claude/Projects/Stock
/usr/bin/python3 -m pip install yfinance pandas requests openpyxl -q 2>/dev/null || true
/usr/bin/python3 set_session_runner.py afternoon >> logs/afternoon_session.log 2>&1
