import yfinance as yf

ticker = yf.Ticker("ZOMATO.NS")
print("shortName:", ticker.info.get("shortName"))
print("longName:", ticker.info.get("longName"))
