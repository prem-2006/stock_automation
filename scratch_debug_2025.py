"""Fetch actual 2025 IPO stocks from NSE and test a few with yfinance."""
import io
import requests
import pandas as pd
import yfinance as yf

# Fetch NSE equity list
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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

if "DATE OF LISTING" in df.columns:
    df["DATE OF LISTING"] = pd.to_datetime(df["DATE OF LISTING"], format="%d-%b-%Y", errors="coerce")
    df["IPO_YEAR"] = df["DATE OF LISTING"].dt.year

# Filter 2025
stocks_2025 = df[df["IPO_YEAR"] == 2025]
print(f"\nFound {len(stocks_2025)} stocks listed in 2025")
print(f"First 10 symbols: {stocks_2025['SYMBOL'].head(10).tolist()}")

# Test first 5 with yfinance
test_symbols = stocks_2025["SYMBOL"].head(5).tolist()
for sym in test_symbols:
    yf_sym = f"{sym}.NS"
    print(f"\n{'='*60}")
    print(f"Testing: {yf_sym}")
    try:
        ticker = yf.Ticker(yf_sym)
        data = ticker.history(period="max", interval="1mo")
        
        if data is not None and not data.empty:
            if isinstance(data.columns, pd.MultiIndex):
                data.columns = data.columns.get_level_values(0)
            
            data = data.dropna(how="all")
            data = data.dropna(subset=["High", "Close"])
            print(f"  Rows: {len(data)}")
            print(f"  First date: {data.index[0]} (year={data.index[0].year})")
            print(f"  Last date: {data.index[-1]}")
            print(f"  First month HIGH: {data['High'].iloc[0]:.2f}")
            
            if len(data) >= 2:
                first_high = data['High'].iloc[0]
                found_breakout = False
                for i in range(1, len(data)):
                    close_val = data['Close'].iloc[i]
                    if close_val > first_high:
                        print(f"  ✓ BREAKOUT at index {i} ({data.index[i].strftime('%Y-%m')}): Close={close_val:.2f} > First High={first_high:.2f}")
                        found_breakout = True
                        break
                if not found_breakout:
                    print(f"  ✗ NO BREAKOUT. Latest close: {data['Close'].iloc[-1]:.2f} vs First High: {first_high:.2f}")
            else:
                print(f"  Only {len(data)} month(s) — insufficient")
        else:
            print(f"  NO DATA returned")
    except Exception as e:
        print(f"  ERROR: {e}")
