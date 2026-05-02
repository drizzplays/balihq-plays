import os
from pathlib import Path
import json

import gspread
from google.oauth2.service_account import Credentials
import requests


DISCORD_EMBED_COLOR = 0x7CFF00
BASE_DIR = Path(__file__).resolve().parent
IMAGES_DIR = BASE_DIR / "images"
AVATAR_PATH = IMAGES_DIR / "avatar.png"
BANNER_PATH = IMAGES_DIR / "banner.png"
BRAND_NAME = "BALIHQBETS"


# Replace this with your real Google Sheet ID or set GOOGLE_SHEET_ID in GitHub Secrets.
DEFAULT_SHEET_ID = "YOUR_SHEET_ID_HERE"


def _get_value(row: dict, *keys: str, fallback: str = "N/A") -> str:
    """Read a sheet value while tolerating small header-name differences."""
    normalized = {str(k).strip().lower(): v for k, v in row.items()}

    for key in keys:
        value = normalized.get(key.strip().lower())
        value = str(value or "").strip()
        if value:
            return value

    return fallback


def _format_unit(unit: str) -> str:
    if unit == "N/A":
        return "Unit: N/A"

    clean_unit = unit.strip()
    if clean_unit.lower().endswith("u"):
        return clean_unit

    return f"{clean_unit}u"


def _build_embed_payload(row: dict, avatar_file_name: str | None = None, banner_file_name: str | None = None) -> dict:
    league = _get_value(row, "LEAGUE", fallback="TT Elite")
    pst = _get_value(row, "PST")
    mtn = _get_value(row, "MTN", "MST")
    est = _get_value(row, "EST")
    player_1 = _get_value(row, "Player 1", "Player1", fallback="TBD")
    player_2 = _get_value(row, "Player 2", "Player2", fallback="TBD")
    bet = _get_value(row, "BET", fallback="No Bet Found")
    unit = _get_value(row, "Unit", "Units")
    history = _get_value(row, "History", "Unit History")

    time_line_parts = []
    if est != "N/A":
        time_line_parts.append(f"{est} EST")
    if mtn != "N/A":
        time_line_parts.append(f"{mtn} MTN")
    if pst != "N/A":
        time_line_parts.append(f"{pst} PST")

    time_line = " | ".join(time_line_parts) if time_line_parts else "N/A"
    unit_text = _format_unit(unit)

    bet_line = f"✅ **{bet}**"
    if history != "N/A":
        bet_line += f" • {history} L20"

    embed: dict = {
        "color": DISCORD_EMBED_COLOR,
        "author": {
            "name": f"🌴 {BRAND_NAME}",
        },
        "description": (
            f"## 🕒 {time_line}\n"
            f"### {player_1} `vs` {player_2}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"### {league}\n"
            f"*1 play*\n\n"
            f"{bet_line}\n"
            f"`{unit_text}`"
        ),
        "footer": {
            "text": f"🌴 {BRAND_NAME}",
        },
    }

    if avatar_file_name:
        avatar_url = f"attachment://{avatar_file_name}"
        embed["author"]["icon_url"] = avatar_url
        embed["thumbnail"] = {"url": avatar_url}
        embed["footer"]["icon_url"] = avatar_url

    if banner_file_name:
        embed["image"] = {"url": f"attachment://{banner_file_name}"}

    return {
        "content": "",
        "embeds": [embed],
    }


def _post_embed_to_discord(webhook_url: str, payload: dict) -> requests.Response:
    files = []
    open_files = []

    try:
        if AVATAR_PATH.exists():
            avatar_file = AVATAR_PATH.open("rb")
            open_files.append(avatar_file)
            files.append(("files[0]", (AVATAR_PATH.name, avatar_file, "image/png")))

        if BANNER_PATH.exists():
            banner_file = BANNER_PATH.open("rb")
            open_files.append(banner_file)
            files.append(("files[1]", (BANNER_PATH.name, banner_file, "image/png")))

        if files:
            return requests.post(
                webhook_url,
                data={"payload_json": json.dumps(payload)},
                files=files,
                timeout=30,
            )

        return requests.post(webhook_url, json=payload, timeout=30)

    finally:
        for file_obj in open_files:
            file_obj.close()


def _get_latest_row(sheet) -> dict | None:
    records = sheet.get_all_records()
    if not records:
        return None

    for row in reversed(records):
        if any(str(value or "").strip() for value in row.values()):
            return row

    return None


def run_automation():
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    sheet_id = os.getenv("GOOGLE_SHEET_ID", DEFAULT_SHEET_ID)

    if not creds_json or not webhook_url:
        print("❌ Error: Missing Environment Variables")
        return

    if not sheet_id or sheet_id == "YOUR_SHEET_ID_HERE":
        print("❌ Error: Missing Google Sheet ID. Set GOOGLE_SHEET_ID in GitHub Secrets or edit DEFAULT_SHEET_ID.")
        return

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds_data = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_data, scopes=scopes)
    client = gspread.authorize(creds)

    try:
        sheet = client.open_by_key(sheet_id).sheet1

        row = _get_latest_row(sheet)
        if not row:
            print("⚠️ Sheet is empty.")
            return

        print(f"✅ Found data for: {row.get('Player 1')} vs {row.get('Player 2')}")

        avatar_file_name = AVATAR_PATH.name if AVATAR_PATH.exists() else None
        banner_file_name = BANNER_PATH.name if BANNER_PATH.exists() else None
        payload = _build_embed_payload(row, avatar_file_name, banner_file_name)

        response = _post_embed_to_discord(webhook_url, payload)

        if response.status_code in (200, 204):
            print("🚀 Success! Play posted to Discord as an embed.")
        else:
            print(f"❌ Failed. Status: {response.status_code}, Response: {response.text}")

    except Exception as e:
        print(f"❌ Python Error: {e}")


if __name__ == "__main__":
    run_automation()
