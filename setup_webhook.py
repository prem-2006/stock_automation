import os
import sys
import requests
from dotenv import load_dotenv

def setup_webhook():
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    
    if not token:
        token = input("Enter your Telegram Bot Token: ").strip()
        
    print("\nFormat: https://your-app-name.koyeb.app")
    url = input("Enter your new Koyeb App URL: ").strip()
    
    # Clean up URL
    url = url.rstrip('/')
    webhook_url = f"{url}/webhook/telegram"
    
    print(f"\nSetting Webhook to: {webhook_url}")
    
    api_url = f"https://api.telegram.org/bot{token}/setWebhook?url={webhook_url}"
    
    response = requests.get(api_url)
    
    if response.status_code == 200:
        print("✅ Success! Webhook updated successfully.")
        print(response.json())
    else:
        print("❌ Failed!")
        print(response.text)

if __name__ == "__main__":
    setup_webhook()
