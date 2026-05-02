import os
from pathlib import Path
import json
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
import requests

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
except ImportError as exc:
    raise RuntimeError("Pillow is required. Add pillow to requirements.txt") from exc


DISCORD_EMBED_COLOR = 0x7CFF00
ROLE_ID = "1500237161335881768"

BASE_DIR = Path(__file__).resolve().parent
IMAGES_DIR = BASE_DIR / "images"
AVATAR_PATH = IMAGES_DIR / "avatar.png"
BANNER_PATH = IMAGES_DIR / "banner.png"
GENERATED_CARD_PATH = BASE_DIR / f"generated_bali_pick_{int(time.time())}.png"

BRAND_NAME = "BALIHQBETS"
DEFAULT_SHEET_ID = "YOUR_SHEET_ID_HERE"

EST_TZ = ZoneInfo("America/New_York")
POST_WINDOW_MINUTES = 8


def _normalize_header(header: str) -> str:
    return str(header or "").strip()


def _rows_from_sheet(sheet):
    values = sheet.get_all_values()
    if len(values) < 2:
        return [], [], {}

    raw_headers = [_normalize_header(h) for h in values[0]]
    seen = {}
    headers = []

    # Supports duplicate headers in Google Sheets:
    # BET | Unit | History | BET | Unit | History
    # becomes:
    # BET | Unit | History | BET 2 | Unit 2 | History 2
    for header in raw_headers:
        if not header:
            headers.append("")
            continue

        key = header.lower()
        seen[key] = seen.get(key, 0) + 1

        if seen[key] == 1:
            headers.append(header)
        else:
            headers.append(f"{header} {seen[key]}")

    rows = []
    row_numbers = []

    for index, row_values in enumerate(values[1:], start=2):
        if not any(str(v or "").strip() for v in row_values):
            continue

        row = {}
        for i, header in enumerate(headers):
            if not header:
                continue
            row[header] = row_values[i] if i < len(row_values) else ""

        rows.append(row)
        row_numbers.append(index)

    header_map = {h.lower(): i + 1 for i, h in enumerate(headers) if h}
    return rows, row_numbers, header_map


def _normalize_row(row: dict) -> dict:
    return {str(k).strip().lower(): v for k, v in row.items()}


def _get_value(row: dict, *keys: str, fallback: str = "N/A") -> str:
    normalized = _normalize_row(row)

    for key in keys:
        value = normalized.get(key.strip().lower())
        value = str(value or "").strip()
        if value:
            return value

    return fallback


def _parse_est_datetime(row: dict) -> datetime | None:
    est_text = _get_value(row, "EST", fallback="").strip()
    if not est_text:
        return None

    clean = est_text.upper().replace("EST", "").replace("EDT", "").strip()
    clean = re.sub(r"\s+", " ", clean)

    today = datetime.now(EST_TZ).date()

    formats = [
        "%I:%M %p",
        "%I:%M%p",
        "%I %p",
        "%I%p",
        "%H:%M",
    ]

    for fmt in formats:
        try:
            parsed_time = datetime.strptime(clean, fmt).time()
            return datetime.combine(today, parsed_time, tzinfo=EST_TZ)
        except ValueError:
            continue

    compact = clean.replace(" ", "")
    for fmt in ["%I:%M%p", "%I%p"]:
        try:
            parsed_time = datetime.strptime(compact, fmt).time()
            return datetime.combine(today, parsed_time, tzinfo=EST_TZ)
        except ValueError:
            continue

    return None


def _is_post_time(row: dict) -> tuple[bool, str]:
    play_time = _parse_est_datetime(row)

    if not play_time:
        return False, "Missing or invalid EST time"

    now = datetime.now(EST_TZ)
    post_at = play_time - timedelta(minutes=POST_WINDOW_MINUTES)

    if post_at <= now <= play_time:
        return True, (
            f"Inside EST post window: "
            f"{post_at.strftime('%I:%M %p')} - {play_time.strftime('%I:%M %p')} EST"
        )

    return False, (
        f"Not time yet. "
        f"Now: {now.strftime('%I:%M %p')} EST | "
        f"Post window: {post_at.strftime('%I:%M %p')} - {play_time.strftime('%I:%M %p')} EST"
    )


def _font(size: int, bold: bool = False):
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]

    for path in paths:
        if Path(path).exists():
            return ImageFont.truetype(path, size)

    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _fit_text(draw: ImageDraw.ImageDraw, text: str, max_width: int, size: int, bold: bool = True, min_size: int = 16):
    text = str(text)

    for font_size in range(size, min_size - 1, -2):
        font = _font(font_size, bold)
        if _text_width(draw, text, font) <= max_width:
            return text, font

    font = _font(min_size, bold)
    ellipsis = "..."
    while len(text) > 3 and _text_width(draw, text + ellipsis, font) > max_width:
        text = text[:-1]

    return text + ellipsis, font


def _rounded_rect(draw: ImageDraw.ImageDraw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _paste_contain(canvas: Image.Image, image_path: Path, box: tuple[int, int, int, int]):
    if not image_path.exists():
        return

    img = Image.open(image_path).convert("RGBA")
    target_w = box[2] - box[0]
    target_h = box[3] - box[1]
    img.thumbnail((target_w, target_h), Image.LANCZOS)

    x = box[0] + (target_w - img.width) // 2
    y = box[1] + (target_h - img.height) // 2
    canvas.alpha_composite(img, (x, y))


def _draw_soft_glow(base: Image.Image, box, radius: int, color=(124, 255, 0, 100), border=4):
    glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(box, radius=radius, outline=color, width=border)
    glow = glow.filter(ImageFilter.GaussianBlur(8))
    base.alpha_composite(glow)


def _draw_check(draw: ImageDraw.ImageDraw, x: int, y: int):
    green = (124, 255, 0)
    _rounded_rect(draw, (x, y, x + 56, y + 56), 12, fill=green, outline=(185, 255, 130), width=2)
    draw.line((x + 14, y + 31, x + 24, y + 41), fill=(255, 255, 255), width=7)
    draw.line((x + 24, y + 41, x + 43, y + 17), fill=(255, 255, 255), width=7)


def _draw_clock(draw: ImageDraw.ImageDraw, x: int, y: int):
    green = (124, 255, 0)
    draw.ellipse((x, y, x + 52, y + 52), outline=green, width=4)
    draw.line((x + 26, y + 10, x + 26, y + 28), fill=green, width=4)
    draw.line((x + 26, y + 28, x + 40, y + 38), fill=green, width=4)


def _collect_plays(row: dict) -> list[dict]:
    normalized = _normalize_row(row)
    plays = []

    def get_any(*names: str) -> str:
        for name in names:
            value = str(normalized.get(name.strip().lower(), "") or "").strip()
            if value:
                return value
        return ""

    first_bet = get_any("bet")
    if first_bet:
        plays.append(
            {
                "bet": first_bet,
                "history": get_any("history", "unit history"),
                "unit": get_any("unit", "units"),
            }
        )

    for i in range(2, 9):
        bet = get_any(f"bet {i}", f"bet{i}", f"bet_{i}", f"play {i}", f"play{i}", f"play_{i}")
        if not bet:
            continue

        plays.append(
            {
                "bet": bet,
                "history": get_any(f"history {i}", f"history{i}", f"history_{i}", f"unit history {i}", f"unit history{i}", f"unit_history_{i}"),
                "unit": get_any(f"unit {i}", f"unit{i}", f"unit_{i}", f"units {i}", f"units{i}", f"units_{i}"),
            }
        )

    if not plays:
        plays.append({"bet": "No Bet Found", "history": "", "unit": get_any("unit", "units")})

    return plays


def _play_label(play: dict) -> str:
    label = str(play.get("bet", "") or "No Bet Found").strip()
    history = str(play.get("history", "") or "").strip()

    if history:
        label += f"  •  {history} L20"

    return label


def _format_unit(unit: str) -> str:
    unit = str(unit or "").strip()
    if not unit:
        return ""
    return unit if unit.lower().endswith("u") else f"{unit}u"


def _generate_pick_card(row: dict) -> Path:
    league = _get_value(row, "LEAGUE", fallback="TT Elite")
    est = _get_value(row, "EST")
    player_1 = _get_value(row, "Player 1", "Player1", fallback="TBD")
    player_2 = _get_value(row, "Player 2", "Player2", fallback="TBD")

    plays = _collect_plays(row)
    play_count = len(plays)
    primary_unit = _format_unit(plays[0].get("unit", "") or _get_value(row, "Unit", "Units", fallback=""))

    width = 1200

    green = (124, 255, 0)
    white = (245, 245, 245)
    dark_bg = (4, 8, 9)
    card_fill = (7, 12, 14)
    top_fill = (9, 15, 16)
    panel_fill = (6, 12, 13)
    row_fill = (10, 16, 18)
    line_soft = (43, 54, 56)
    border_soft = (52, 62, 64)
    accent_grey = (120, 130, 134)

    brand_top = 34
    top_bar_top = 112
    top_bar_h = 94
    top_bar_x1 = 42
    top_bar_x2 = 926

    panel_x1 = 42
    panel_x2 = width - 42
    panel_top = 252

    league_top = panel_top + 46
    play_count_y = panel_top + 160

    rows_top = panel_top + 214
    row_h = 78
    row_gap = 12

    rows_bottom = rows_top + play_count * (row_h + row_gap) - row_gap
    unit_y = rows_bottom + 14

    banner_top = unit_y + 40
    banner_h = 240

    panel_bottom = banner_top + banner_h + 34
    height = panel_bottom + 24

    img = Image.new("RGBA", (width, height), (*dark_bg, 255))
    draw = ImageDraw.Draw(img)

    # Dotted texture
    for x in range(0, width, 26):
        for y in range(0, height, 26):
            draw.ellipse((x, y, x + 2, y + 2), fill=(18, 30, 31, 95))

    # Outer card
    _rounded_rect(draw, (22, 16, width - 22, height - 16), 22, fill=card_fill, outline=border_soft, width=2)

    # Brand row
    _paste_contain(img, AVATAR_PATH, (44, brand_top, 92, brand_top + 48))
    draw.text((106, brand_top - 1), BRAND_NAME, font=_font(30, True), fill=white)
    _paste_contain(img, AVATAR_PATH, (972, 26, 1088, 126))

    # Top matchup card
    top_bar = (top_bar_x1, top_bar_top, top_bar_x2, top_bar_top + top_bar_h)
    _draw_soft_glow(img, top_bar, radius=18)
    _rounded_rect(draw, top_bar, 18, fill=top_fill, outline=green, width=2)

    _draw_clock(draw, 60, top_bar_top + 20)

    time_text, time_font = _fit_text(draw, est, 150, 28, True, 20)
    draw.text((132, top_bar_top + 16), time_text, font=time_font, fill=white)
    draw.text((142, top_bar_top + 50), "EST", font=_font(18, True), fill=green)

    draw.line((246, top_bar_top + 16, 246, top_bar_top + top_bar_h - 16), fill=accent_grey, width=2)

    matchup = f"{player_1} vs {player_2}"
    matchup_text, matchup_font = _fit_text(draw, matchup, 590, 22, True, 16)

    if " vs " in matchup_text and not matchup_text.endswith("..."):
        p1, p2 = matchup_text.split(" vs ", 1)
        p1_w = _text_width(draw, p1 + " ", matchup_font)
        vs_w = _text_width(draw, "vs ", matchup_font)
        draw.text((274, top_bar_top + 34), p1 + " ", font=matchup_font, fill=white)
        draw.text((274 + p1_w, top_bar_top + 34), "vs ", font=matchup_font, fill=green)
        draw.text((274 + p1_w + vs_w, top_bar_top + 34), p2, font=matchup_font, fill=white)
    else:
        draw.text((274, top_bar_top + 34), matchup_text, font=matchup_font, fill=white)

    # Main panel
    panel_box = (panel_x1, panel_top, panel_x2, panel_bottom)
    _draw_soft_glow(img, panel_box, radius=24)
    _rounded_rect(draw, panel_box, 24, fill=panel_fill, outline=green, width=2)

    flag_x = 86
    flag_y = league_top + 8
    draw.rectangle((flag_x, flag_y, flag_x + 76, flag_y + 56), fill=(235, 235, 235))
    draw.rectangle((flag_x, flag_y + 28, flag_x + 76, flag_y + 56), fill=(235, 25, 45))

    league_text, league_font = _fit_text(draw, league.upper(), 520, 42, True, 28)
    draw.text((188, league_top), league_text, font=league_font, fill=white)

    line_y = league_top + 88
    draw.line((188, line_y, 620, line_y), fill=green, width=3)
    draw.line((620, line_y, 662, line_y - 30), fill=green, width=3)

    play_word = "play" if play_count == 1 else "plays"
    draw.text((86, play_count_y), f"{play_count} {play_word}", font=_font(28, True), fill=green)

    row_x1 = 94
    row_x2 = width - 94

    def play_row(y: int, label: str):
        _rounded_rect(draw, (row_x1, y, row_x2, y + row_h), 12, fill=row_fill, outline=line_soft, width=1)
        _draw_check(draw, row_x1 + 20, y + 11)
        fitted_label, fitted_font = _fit_text(draw, label, 780, 28, True, 18)
        draw.text((row_x1 + 96, y + 21), fitted_label, font=fitted_font, fill=white)

    current_y = rows_top
    for play in plays:
        play_row(current_y, _play_label(play))
        current_y += row_h + row_gap

    if primary_unit:
        _rounded_rect(draw, (86, unit_y, 148, unit_y + 28), 4, fill=(11, 20, 16), outline=green, width=2)
        draw.text((102, unit_y + 1), primary_unit, font=_font(19, True), fill=green)

    _paste_contain(img, BANNER_PATH, (row_x1, banner_top, row_x2, banner_top + banner_h))

    img = img.convert("RGB")
    img.save(GENERATED_CARD_PATH, quality=95)
    return GENERATED_CARD_PATH


def _build_embed_payload(card_file_name: str, avatar_file_name: str | None = None) -> dict:
    embed = {
        "color": DISCORD_EMBED_COLOR,
        "image": {"url": f"attachment://{card_file_name}"},
        "footer": {"text": BRAND_NAME},
    }

    if avatar_file_name:
        avatar_url = f"attachment://{avatar_file_name}"
        embed["footer"]["icon_url"] = avatar_url

    return {
        "content": f"<@&{ROLE_ID}>",
        "allowed_mentions": {"roles": [ROLE_ID]},
        "embeds": [embed],
    }


def _post_card_to_discord(webhook_url: str, card_path: Path) -> requests.Response:
    avatar_file_name = AVATAR_PATH.name if AVATAR_PATH.exists() else None
    payload = _build_embed_payload(card_path.name, avatar_file_name)

    open_files = []
    files = []

    try:
        card_file = card_path.open("rb")
        open_files.append(card_file)
        files.append(("files[0]", (card_path.name, card_file, "image/png")))

        if AVATAR_PATH.exists():
            avatar_file = AVATAR_PATH.open("rb")
            open_files.append(avatar_file)
            files.append(("files[1]", (AVATAR_PATH.name, avatar_file, "image/png")))

        return requests.post(webhook_url, data={"payload_json": json.dumps(payload)}, files=files, timeout=30)

    finally:
        for file_obj in open_files:
            file_obj.close()


def _ensure_posted_column(sheet, header_map: dict) -> int:
    if "posted" in header_map:
        return header_map["posted"]

    next_col = max(header_map.values(), default=0) + 1
    sheet.update_cell(1, next_col, "POSTED")
    return next_col


def _mark_posted(sheet, row_number: int, posted_col: int):
    now = datetime.now(EST_TZ).strftime("%Y-%m-%d %I:%M %p EST")
    sheet.update_cell(row_number, posted_col, now)


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
        rows, row_numbers, header_map = _rows_from_sheet(sheet)

        if not rows:
            print("⚠️ Sheet is empty.")
            return

        posted_col = _ensure_posted_column(sheet, header_map)

        for row, row_number in zip(rows, row_numbers):
            posted_value = _get_value(row, "POSTED", fallback="").strip()

            if posted_value:
                continue

            should_post, reason = _is_post_time(row)
            print(f"Row {row_number}: {reason}")

            if not should_post:
                continue

            print(f"✅ Posting play for: {row.get('Player 1')} vs {row.get('Player 2')}")

            card_path = _generate_pick_card(row)
            response = _post_card_to_discord(webhook_url, card_path)

            if response.status_code in (200, 204):
                _mark_posted(sheet, row_number, posted_col)
                print("🚀 Success! Visual play card posted to Discord and row marked POSTED.")
            else:
                print(f"❌ Failed. Status: {response.status_code}, Response: {response.text}")

            return

        print("ℹ️ No eligible plays to post right now.")

    except Exception as e:
        print(f"❌ Python Error: {e}")


if __name__ == "__main__":
    run_automation()
