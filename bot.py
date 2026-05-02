import json
import os
from datetime import datetime, timezone
from collections import defaultdict

import gspread
import requests
from google.oauth2.service_account import Credentials


# Required GitHub secrets:
# - GOOGLE_SERVICE_ACCOUNT_JSON
# - DISCORD_WEBHOOK_URL
# - GOOGLE_SHEET_ID
#
# Optional GitHub secrets / env vars:
# - SHEET_NAME                 Defaults to first sheet tab
# - DISCORD_USERNAME           Defaults to BaliHQ Plays
# - DISCORD_AVATAR_URL         Optional webhook avatar URL
# - DISCORD_EMBED_COLOR        Decimal or hex, ex: 3447003 or 0x3498db
# - COUNTRY_FLAG               Defaults to 🇵🇱

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

POSTED_HEADER = "Posted"
MAX_FIELDS_PER_EMBED = 25


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def clean(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def first_value(row: dict, *keys: str, default: str = "") -> str:
    """Return the first non-empty value from a row, accepting multiple possible headers."""
    lower_map = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        value = lower_map.get(key.lower())
        if clean(value):
            return clean(value)
    return default


def parse_color(value: str | None) -> int:
    if not value:
        return 0x2F80ED
    value = value.strip()
    if value.lower().startswith("0x"):
        return int(value, 16)
    if value.startswith("#"):
        return int(value[1:], 16)
    return int(value)


def get_sheet():
    creds_json = require_env("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = require_env("GOOGLE_SHEET_ID")
    sheet_name = os.getenv("SHEET_NAME")

    creds_data = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_data, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(sheet_id)

    if sheet_name:
        return spreadsheet.worksheet(sheet_name)
    return spreadsheet.sheet1


def ensure_posted_column(sheet) -> int:
    headers = sheet.row_values(1)
    normalized = [h.strip().lower() for h in headers]

    if POSTED_HEADER.lower() in normalized:
        return normalized.index(POSTED_HEADER.lower()) + 1

    next_col = len(headers) + 1
    sheet.update_cell(1, next_col, POSTED_HEADER)
    return next_col


def latest_unposted_groups(sheet):
    posted_col = ensure_posted_column(sheet)
    records = sheet.get_all_records()

    unposted = []
    for idx, row in enumerate(records, start=2):
        posted_value = first_value(row, POSTED_HEADER)
        bet = first_value(row, "BET", "Play", "Pick", "Prop")
        if not posted_value and bet:
            unposted.append({"sheet_row": idx, **row})

    groups = defaultdict(list)
    for row in unposted:
        key = (
            first_value(row, "EST", "Time EST", "Start EST", default="TBD"),
            first_value(row, "PST", "Time PST", "Start PST", default=""),
            first_value(row, "Player 1", "Player1", "P1", default="TBD"),
            first_value(row, "Player 2", "Player2", "P2", default="TBD"),
            first_value(row, "LEAGUE", "League", "Competition", default="Bet"),
        )
        groups[key].append(row)

    return groups, posted_col


def format_play(row: dict) -> tuple[str, str]:
    player = first_value(row, "Player", "Selection", "Target", "Name")
    bet = first_value(row, "BET", "Play", "Pick", "Prop", default="No Bet Found")
    history = first_value(row, "Unit History", "History", "Record", "Trend", "Line", "Streak")
    odds = first_value(row, "Odds", "Price")
    confidence = first_value(row, "Confidence", "%", "Percent", "Hit Rate")

    # If the sheet does not have a separate Player/Selection column, keep BET as the main line.
    name = player if player else bet

    details = []
    if player:
        details.append(bet)
    if history:
        details.append(history)
    if odds:
        details.append(f"Odds: {odds}")
    if confidence:
        details.append(f"Confidence: {confidence}")

    value = "\n".join(details) if details else "—"
    return name[:256], value[:1024]


def build_embed(event_key: tuple, rows: list[dict]) -> dict:
    est, pst, player_1, player_2, league = event_key
    flag = os.getenv("COUNTRY_FLAG", "🇵🇱")

    time_line = f"{est} EST" if est != "TBD" else "TBD"
    if pst:
        time_line += f" | {pst} PST"

    embed = {
        "title": f"{time_line} | {player_1} vs {player_2}",
        "description": f"{flag} *{league}*\n**{len(rows)} play{'s' if len(rows) != 1 else ''}**",
        "color": parse_color(os.getenv("DISCORD_EMBED_COLOR")),
        "fields": [],
        "footer": {"text": "BaliHQ Plays"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    for row in rows[:MAX_FIELDS_PER_EMBED]:
        name, value = format_play(row)
        embed["fields"].append({
            "name": f"🔥 {name}",
            "value": value,
            "inline": False,
        })

    if len(rows) > MAX_FIELDS_PER_EMBED:
        embed["fields"].append({
            "name": "More plays",
            "value": f"{len(rows) - MAX_FIELDS_PER_EMBED} additional plays were not shown because Discord embeds max out at 25 fields.",
            "inline": False,
        })

    return embed


def post_to_discord(embed: dict):
    webhook_url = require_env("DISCORD_WEBHOOK_URL")

    payload = {
        "username": os.getenv("DISCORD_USERNAME", "BaliHQ Plays"),
        "embeds": [embed],
    }

    avatar_url = os.getenv("DISCORD_AVATAR_URL")
    if avatar_url:
        payload["avatar_url"] = avatar_url

    response = requests.post(webhook_url, json=payload, timeout=20)

    if response.status_code not in (200, 204):
        raise RuntimeError(f"Discord post failed: {response.status_code} - {response.text}")


def mark_rows_posted(sheet, posted_col: int, rows: list[dict]):
    posted_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    updates = []

    for row in rows:
        updates.append({
            "range": gspread.utils.rowcol_to_a1(row["sheet_row"], posted_col),
            "values": [[posted_at]],
        })

    if updates:
        sheet.batch_update(updates)


def run_automation():
    sheet = get_sheet()
    groups, posted_col = latest_unposted_groups(sheet)

    if not groups:
        print("✅ No new unposted plays found.")
        return

    for event_key, rows in groups.items():
        embed = build_embed(event_key, rows)
        print(f"🚀 Posting {len(rows)} play(s): {embed['title']}")
        post_to_discord(embed)
        mark_rows_posted(sheet, posted_col, rows)
        print("✅ Posted and marked rows as posted.")


if __name__ == "__main__":
    run_automation()
