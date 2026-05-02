import os
import json
import gspread
import requests
import time

# --- BRANDING CONFIGURATION ---
AVATAR_URL = "YOUR_AVATAR_IMAGE_URL_HERE" # Name: avatar.png
BANNER_URL = "YOUR_BANNER_IMAGE_URL_HERE" # Name: banner.png
USERNAME = "BaliHQ Picks"
COLOR = 0x00FF00 # Neon Green

def send_discord_embed(data):
    url = os.environ['DISCORD_WEBHOOK_URL']
    
    # Constructing the Embed
    embed = {
        "username": USERNAME,
        "avatar_url": AVATAR_URL,
        "embeds": [{
            "title": f"🔥 {data['League']} Green Tier Play",
            "description": f"**{data['Player 1']} vs {data['Player 2']}**",
            "color": COLOR,
            "fields": [
                {"name": "Pick", "value": f"```{data['BET']}```", "inline": True},
                {"name": "Wager", "value": f"```{data['Unit']} Units```", "inline": True},
                {"name": "History", "value": f"{data['History']}", "inline": False},
                {"name": "Time (EST)", "value": f"{data['EST']}", "inline": True}
            ],
            "image": {"url": BANNER_URL},
            "footer": {"text": "BaliHQ Automation • Data Driven"}
        }]
    }
    
    response = requests.post(url, json=embed)
    if response.status_code != 204:
        print(f"Error: {response.status_code}, {response.text}")
    time.sleep(1)

def run_bot():
    # Auth
    creds = json.loads(os.environ['GOOGLE_SHEETS_CREDS'])
    gc = gspread.service_account_from_dict(creds)
    sh = gc.open_by_key(os.environ['SHEET_ID'])
    
    # Target specific tab
    try:
        ws = sh.worksheet("Plays")
        records = ws.get_all_records()
        
        for row in records:
            # We assume every row in the 'Plays' tab is a verified play to post
            # If you want to filter by a 'Posted' column, you could add that here
            
            payload = {
                "League": row.get("League", "MLB"),
                "EST": row.get("EST", "N/A"),
                "Player 1": row.get("Player 1", "TBD"),
                "Player 2": row.get("Player 2", "TBD"),
                "BET": row.get("BET", "No Bet"),
                "Unit": row.get("Unit", "0"),
                "History": row.get("History", "No Data")
            }
            
            send_discord_embed(payload)
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    run_bot()
