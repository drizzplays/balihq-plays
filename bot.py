import os
import gspread
from google.oauth2.service_account import Credentials
import requests
import json

def run_automation():
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    
    if not creds_json or not webhook_url:
        print("❌ Error: Missing Environment Variables")
        return

    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds_data = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_data, scopes=scopes)
    client = gspread.authorize(creds)

    try:
        # 1. Open the Sheet
        # Replace the ID below with your actual Google Sheet ID
        sheet_id = "YOUR_SHEET_ID_HERE" 
        sheet = client.open_by_key(sheet_id).sheet1
        
        # 2. Get the latest row
        records = sheet.get_all_records()
        if not records:
            print("⚠️ Sheet is empty.")
            return
            
        row = records[-1] # The most recent entry
        print(f"✅ Found data for: {row.get('Player 1')} vs {row.get('Player 2')}")

        # 3. Format the Message using your specific headers
        message_content = (
            f"🏆 **NEW {row.get('LEAGUE', 'Bet')} ALERT** 🏆\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ **Time:** {row.get('EST', 'N/A')} EST | {row.get('PST', 'N/A')} PST\n"
            f"👤 **Matchup:** {row.get('Player 1', 'TBD')} vs {row.get('Player 2', 'TBD')}\n"
            f"🔥 **BET:** {row.get('BET', 'No Bet Found')}\n"
            f"📈 **History:** {row.get('Unit History', 'N/A')}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )

        # 4. Send to Discord Webhook
        payload = {"content": message_content}
        response = requests.post(webhook_url, json=payload)
        
        if response.status_code == 204:
            print("🚀 Success! Play posted to Discord.")
        else:
            print(f"❌ Failed. Status: {response.status_code}, Response: {response.text}")

    except Exception as e:
        print(f"❌ Python Error: {e}")

if __name__ == "__main__":
    run_automation()
