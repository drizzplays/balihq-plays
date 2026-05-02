import os
import gspread
from google.oauth2.service_account import Credentials
import requests
import json

def run_automation():
    # 1. Load Google Credentials from Environment Variable
    # This keeps your JSON key secure in your CI/CD secrets
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        print("Error: GOOGLE_SERVICE_ACCOUNT_JSON not found.")
        return

    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds_data = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_data, scopes=scopes)
    client = gspread.authorize(creds)

    try:
        # 2. Open the Sheet (Use your Sheet ID from the URL)
        sheet_id = "YOUR_SHEET_ID_HERE" 
        sheet = client.open_by_key(sheet_id).sheet1 # Opens the first tab
        
        # 3. Get Data (Example: Get the last row of data)
        records = sheet.get_all_records()
        if not records:
            print("Sheet is empty.")
            return
            
        latest_play = records[-1] # Grabs the last row
        
        # 4. Format the Message
        # Customize these keys to match your Sheet headers (e.g., 'Player', 'Prop')
        message = (
            f"🚀 **New Sheet Update** 🚀\n"
            f"**Event:** {latest_play.get('Event', 'N/A')}\n"
            f"**Play:** {latest_play.get('Play', 'N/A')}\n"
            f"**Odds:** {latest_play.get('Odds', 'N/A')}"
        )

        # 5. Send to Discord Webhook
        webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
        payload = {"content": message}
        
        response = requests.post(webhook_url, json=payload)
        
        if response.status_code == 204:
            print("Successfully posted to Discord via Webhook.")
        else:
            print(f"Failed to post. Status: {response.status_code}")

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    run_automation()
