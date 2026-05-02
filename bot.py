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
DEFAULT_SHEET_ID = "12jKPYa58oYAOyfj8FP5v6VrMZe4gVybR7zSq1T4bkBE"


def _clean(value, fallback="N/A"):
    value = str(value or "").strip()
    return value if value else fallback


def _normalize_row(row: dict) -> dict:
    """Normalize header keys so minor sheet-header differences do not break the bot."""
    normalized = {}
    for key, value in (row or {}).items():
        normalized[str(key).strip().lower()] = value
    return normalized


def _pick(row: dict, *keys, fallback="N/A"):
    for key in keys:
        if key in row:
            value = str(row.get(key) or "").strip()
            if value:
                return value
    return fallback


def _build_embed_payload(row: dict, avatar_file_name: str | None = None, banner_file_name: str | None = None) -> dict:
    normalized = _normalize_row(row)

    league = _pick(normalized, "league", fallback="Bet Alert")
    pst = _pick(normalized, "pst")
    mtn = _pick(normalized, "mtn", "mst")
    est = _pick(normalized, "est")
    player_1 = _pick(normalized, "player 1", "player1", fallback="TBD")
    player_2 = _pick(normalized, "player 2", "player2", fallback="TBD")
    bet = _pick(normalized, "bet", fallback="No Bet Found")
    unit = _pick(normalized, "unit", "units")
    history = _pick(normalized, "history", "unit history")

    # Cleaner layout: less clutter, actual sheet columns displayed, and no fake N/A when valid headers exist.
    embed: dict = {
        "color": DISCORD_EMBED_COLOR,
        "author": {
            "name": BRAND_NAME,
        },
        "title": "🏆 NEW BET ALERT 🏆",
        "description": f"**League:** {league}",
        "fields": [
            {
                "name": "⏰ PST",
                "value": pst,
                "inline": True,
            },
            {
                "name": "⛰️ MTN",
                "value": mtn,
                "inline": True,
            },
            {
                "name": "🕒 EST",
                "value": est,
                "inline": True,
            },
            {
                "name": "🎯 Matchup",
                "value": f"{player_1} vs {player_2}",
                "inline": False,
            },
            {
                "name": "🔥 Bet",
                "value": bet,
                "inline": True,
            },
            {
                "name": "💰 Unit",
                "value": unit,
                "inline": True,
            },
            {
                "name": "📈 History",
                "value": history,
                "inline": False,
            },
        ],
        "footer": {
            "text": BRAND_NAME,
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
    """Return the latest row with at least one non-empty value."""
    records = sheet.get_all_records()
    if not records:
        return None

    for row in reversed(records):
        if any(str(v or "").strip() for v in row.values()):
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
