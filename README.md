# 📊 IPO Breakout Stock Screener

A production-ready WhatsApp bot that screens NSE-listed stocks by IPO year, identifies stocks that broke above their first-month listing high, generates professional Excel reports, and delivers results via WhatsApp.

---

## 🏗️ Architecture

```
WhatsApp Message (Twilio)
        ↓
  POST /webhook/whatsapp
        ↓
  Conversation State Machine
  (greeting → year input → processing → completed)
        ↓
  Scanner Service (ThreadPoolExecutor)
        ↓
  yfinance API (Monthly OHLC Data)
        ↓
  Breakout Condition Check
  (Monthly Close > IPO First Month High)
        ↓
  Excel Report Generation (openpyxl)
        ↓
  WhatsApp Reply + Excel Attachment
```

## 📋 Screening Logic

For each stock listed in the entered IPO year:

1. Fetch monthly OHLC data from listing date to current date
2. Record the **first listed month's HIGH** as `IPO_FIRST_MONTH_HIGH`
3. Check every subsequent monthly candle:
   - **IF** `Monthly_Close > IPO_FIRST_MONTH_HIGH` **→ Stock Qualifies**
4. Record the breakout month, close price, current price, and % gain

---

## 🚀 Quick Start

### Prerequisites

- Python 3.10+
- Twilio account with WhatsApp sandbox (for WhatsApp integration)
- ngrok (for local development webhooks)

### 1. Clone & Setup

```bash
cd c:\Users\prems\OneDrive\Desktop\st

# Create virtual environment
python -m venv venv

# Activate (Windows)
venv\Scripts\activate

# Activate (macOS/Linux)
# source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
# Copy the example env file
copy .env.example .env

# Edit .env with your settings
# At minimum, set these for WhatsApp:
#   TWILIO_ACCOUNT_SID=your_sid
#   TWILIO_AUTH_TOKEN=your_token
#   TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886
#   BASE_URL=https://your-ngrok-url.ngrok-free.app
```

> **Note:** The app works without Twilio credentials — it will log messages instead of sending them. You can use the REST API endpoints directly.

### 3. Run the Server

```bash
python run.py
```

The server starts at `http://localhost:8000`

- **API Docs:** http://localhost:8000/docs
- **Health Check:** http://localhost:8000/health

---

## 📱 WhatsApp Setup (Twilio)

### Development (Sandbox)

1. Go to [Twilio Console → WhatsApp Sandbox](https://console.twilio.com/us1/develop/sms/try-it-out/whatsapp-learn)
2. Join the sandbox by sending the provided code to the sandbox number
3. Set the webhook URL to: `https://your-ngrok-url.ngrok-free.app/webhook/whatsapp`
4. Start ngrok: `ngrok http 8000`

### Production

1. Apply for a [Twilio WhatsApp Business Profile](https://www.twilio.com/docs/whatsapp)
2. Configure your production URL as the webhook endpoint
3. Set `BASE_URL` in `.env` to your production domain

---

## 🔌 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Application info |
| `GET` | `/health` | Health check |
| `POST` | `/webhook/whatsapp` | Twilio WhatsApp webhook |
| `POST` | `/scan` | Trigger manual scan |
| `GET` | `/scan/{scan_id}` | Check scan status |
| `GET` | `/scans` | List recent scans |
| `GET` | `/reports/{filename}` | Download Excel report |
| `GET` | `/docs` | Interactive API docs |

### Manual Scan (without WhatsApp)

```bash
# Trigger a scan
curl -X POST http://localhost:8000/scan \
  -H "Content-Type: application/json" \
  -d '{"year": 2020}'

# Check status
curl http://localhost:8000/scan/{scan_id}

# Download report
curl -O http://localhost:8000/reports/{filename}
```

---

## 📊 Excel Report Structure

### Sheet 1: Qualified Stocks
| Column | Description |
|--------|-------------|
| Symbol | NSE trading symbol |
| Company Name | Full company name |
| IPO Year | Year of listing |
| IPO First Month High | First month's HIGH price |
| Breakout Month | Month when close exceeded IPO high |
| Breakout Close | Close price at breakout |
| Current Price | Latest available price |
| % Above IPO High | Current gain over IPO high |
| Listing Date | NSE listing date |

### Sheet 2: All Stocks
All scanned stocks with qualification status (green = qualified, red = not qualified)

### Sheet 3: Summary
- Total stocks scanned
- Qualified count and percentage
- Top 10 strongest stocks (with gold/silver/bronze highlighting)

---

## 🗄️ Database Schema

SQLite database at `db/stock_screener.db`:

| Table | Purpose |
|-------|---------|
| `stocks` | Cached NSE equity master list |
| `scan_jobs` | Scan job tracking (status, progress) |
| `scan_results` | Individual stock screening results |
| `conversations` | WhatsApp conversation state machine |
| `cached_stock_data` | Cached yfinance OHLC data |

---

## 🐳 Docker Deployment

### Build & Run

```bash
# Build and start
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

### Production Docker

```bash
# Build image
docker build -t ipo-screener .

# Run with env file
docker run -d \
  --name ipo-screener \
  -p 8000:8000 \
  --env-file .env \
  -v $(pwd)/db:/app/db \
  -v $(pwd)/data:/app/data \
  ipo-screener
```

---

## 🧪 Testing

```bash
# Run all tests
python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_scanner.py -v

# Run with coverage
python -m pytest tests/ --cov=app --cov-report=html
```

---

## 📁 Project Structure

```
st/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI application
│   ├── config.py               # Environment configuration
│   ├── database.py             # SQLite/SQLAlchemy setup
│   ├── models.py               # ORM models
│   ├── schemas.py              # Pydantic schemas
│   ├── routers/
│   │   ├── webhook.py          # WhatsApp webhook handler
│   │   └── scan.py             # REST scan API
│   ├── services/
│   │   ├── nse_service.py      # NSE stock list fetcher
│   │   ├── scanner_service.py  # Breakout screening engine
│   │   ├── excel_service.py    # Excel report generator
│   │   └── whatsapp_service.py # Twilio WhatsApp integration
│   ├── utils/
│   │   ├── cache.py            # Caching (file + DB)
│   │   └── logger.py           # Structured logging
│   └── static/
│       └── reports/            # Generated Excel files
├── data/                       # Cached NSE data
├── db/                         # SQLite database
├── logs/                       # Application logs
├── tests/
│   ├── test_scanner.py         # Scanner logic tests
│   ├── test_webhook.py         # Webhook integration tests
│   └── test_excel.py           # Excel generation tests
├── .env.example                # Environment template
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── run.py                      # Production launcher
└── README.md
```

---

## ⚙️ Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `TWILIO_ACCOUNT_SID` | `""` | Twilio Account SID |
| `TWILIO_AUTH_TOKEN` | `""` | Twilio Auth Token |
| `TWILIO_WHATSAPP_NUMBER` | `whatsapp:+14155238886` | Twilio WhatsApp number |
| `BASE_URL` | `http://localhost:8000` | Public server URL |
| `HOST` | `0.0.0.0` | Server bind address |
| `PORT` | `8000` | Server port |
| `DATABASE_URL` | `sqlite:///./db/stock_screener.db` | Database connection string |
| `MAX_WORKERS` | `10` | Parallel scanning threads |
| `API_CALL_DELAY` | `0.5` | Delay between API calls (seconds) |
| `MAX_RETRIES` | `3` | Max retry attempts for failed API calls |
| `CACHE_TTL_HOURS` | `24` | Cache expiry (hours) |
| `LOG_LEVEL` | `INFO` | Logging level |

---

## 🚀 Production Deployment Guide

### Option 1: Railway / Render

1. Push code to GitHub
2. Connect repo to Railway/Render
3. Set environment variables in dashboard
4. Deploy — the `Dockerfile` handles everything

### Option 2: AWS EC2

```bash
# SSH into your instance
ssh ec2-user@your-instance

# Install Docker
sudo yum install docker -y
sudo service docker start

# Clone and deploy
git clone your-repo
cd your-repo
cp .env.example .env
# Edit .env with production values
docker-compose up -d
```

### Option 3: Google Cloud Run

```bash
# Build and push
gcloud builds submit --tag gcr.io/PROJECT_ID/ipo-screener

# Deploy
gcloud run deploy ipo-screener \
  --image gcr.io/PROJECT_ID/ipo-screener \
  --port 8000 \
  --allow-unauthenticated \
  --set-env-vars "BASE_URL=https://your-service-url"
```

### SSL/HTTPS

For production, use a reverse proxy (nginx/Caddy) with Let's Encrypt SSL, or deploy to a platform that provides HTTPS automatically (Railway, Render, Cloud Run).

---

## 📝 License

MIT License — feel free to use, modify, and distribute.
