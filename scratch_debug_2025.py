"""Quick debug: test one 2025 IPO stock to see what yfinance returns."""
import yfinance as yf
import pandas as pd

# Some known 2025 IPOs
test_symbols = ["BAJAJHOUSING.NS", "NTPC GREEN.NS", "QUADRANT.NS"]

for sym in test_symbols:
    print(f"\n{'='*60}")
    print(f"Testing: {sym}")
    try:
        ticker = yf.Ticker(sym)
        data = ticker.history(period="max", interval="1mo")
        
        if data is not None and not data.empty:
            if isinstance(data.columns, pd.MultiIndex):
                data.columns = data.columns.get_level_values(0)
            
            data = data.dropna(how="all")
            print(f"  Rows: {len(data)}")
            print(f"  First date: {data.index[0]}")
            print(f"  First year: {data.index[0].year}")
            print(f"  First month HIGH: {data['High'].iloc[0]:.2f}")
            if len(data) >= 2:
                print(f"  2nd month CLOSE: {data['Close'].iloc[1]:.2f}")
                first_high = data['High'].iloc[0]
                for i in range(1, len(data)):
                    if data['Close'].iloc[i] > first_high:
                        print(f"  BREAKOUT at month {i}: Close={data['Close'].iloc[i]:.2f} > First High={first_high:.2f}")
                        break
                else:
                    print(f"  NO BREAKOUT found. Latest close: {data['Close'].iloc[-1]:.2f}")
            else:
                print(f"  Only {len(data)} month(s) of data — insufficient")
        else:
            print(f"  NO DATA returned")
    except Exception as e:
        print(f"  ERROR: {e}")
