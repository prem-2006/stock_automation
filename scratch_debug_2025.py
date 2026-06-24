"""Debug: test 2025 IPO stocks breakout logic."""
import io, sys, os
sys.stdout.reconfigure(encoding='utf-8')

import requests
import pandas as pd
import yfinance as yf
import math

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.nseindia.com/",
}

print("Fetching NSE equity list...")
session = requests.Session()
session.headers.update(NSE_HEADERS)
try:
    session.get("https://www.nseindia.com", timeout=10)
except:
    pass

resp = session.get("https://archives.nseindia.com/content/equities/EQUITY_L.csv", timeout=30)
df = pd.read_csv(io.StringIO(resp.text))
df.columns = df.columns.str.strip()
df["DATE OF LISTING"] = pd.to_datetime(df["DATE OF LISTING"], format="%d-%b-%Y", errors="coerce")
df["IPO_YEAR"] = df["DATE OF LISTING"].dt.year

stocks_2025 = df[df["IPO_YEAR"] == 2025]
print(f"Found {len(stocks_2025)} stocks listed in 2025\n")

# Test first 10 stocks
test_symbols = stocks_2025["SYMBOL"].head(10).tolist()
qualified_count = 0
rejected_older = 0
no_breakout = 0
no_data = 0

for sym in test_symbols:
    yf_sym = f"{sym}.NS"
    try:
        ticker = yf.Ticker(yf_sym)
        data = ticker.history(period="max", interval="1mo")
        
        if data is None or data.empty:
            print(f"  {sym}: NO DATA")
            no_data += 1
            continue
            
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)
        
        data = data.dropna(how="all").dropna(subset=["High", "Close"])
        
        if len(data) < 2:
            print(f"  {sym}: Only {len(data)} months")
            no_data += 1
            continue
        
        first_year = data.index[0].year
        first_high = float(data['High'].iloc[0])
        latest_close = float(data['Close'].iloc[-1])
        
        if first_year < 2025:
            print(f"  {sym}: REJECTED - yfinance first date year={first_year} < 2025 (older stock)")
            rejected_older += 1
            continue
        
        # Check breakout
        found = False
        for i in range(1, len(data)):
            c = float(data['Close'].iloc[i])
            if not math.isnan(c) and c > first_high:
                print(f"  {sym}: QUALIFIED - Breakout at {data.index[i].strftime('%Y-%m')} (Close {c:.2f} > First High {first_high:.2f})")
                found = True
                qualified_count += 1
                break
        
        if not found:
            print(f"  {sym}: NO BREAKOUT - Latest close {latest_close:.2f} vs First High {first_high:.2f}")
            no_breakout += 1
            
    except Exception as e:
        print(f"  {sym}: ERROR - {e}")
        no_data += 1

print(f"\n--- Summary of first 10 ---")
print(f"Qualified: {qualified_count}")
print(f"Rejected (older): {rejected_older}")
print(f"No breakout: {no_breakout}")
print(f"No data/error: {no_data}")
