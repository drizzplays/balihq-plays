import os
from pathlib import Path
import json

import gspread
from google.oauth2.service_account import Credentials
import requests

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
except ImportError as exc:
    raise RuntimeError("Pillow is required. Add pillow to requirements.txt") from exc


DISCORD_EMBED_COLOR = 0x7CFF00
BASE_DIR = Path(__file__).resolve().parent
IMAGES_DIR = BASE_DIR / "images"
AVATAR_PATH = IMAGES_DIR / "avatar.png"
BANNER_PATH = IMAGES_DIR / "banner.png"
GENERATED_CARD_PATH = BASE_DIR / "generated_bali_pick.png"
BRAND_NAME = "BALIHQBETS"

# Replace this with your real Google Sheet ID or set GOOGLE_SHEET_ID in GitHub Secrets.
DEFAULT_SHEET_ID = "YOUR_SHEET_ID_HERE"


def _get_value(row: dict, *keys: str, fallback: str = "N/A") -> str:
    normalized = {str(k).strip().lower(): v for k, v in row.items()}

    for key in keys:
        value = normalized.get(key.strip().lower())
        value = str(value or "").strip()
        if value:
            return value

    return fallback


def _font(size: int, bold: bool = False):
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]

    for path in font_paths:
        if Path(path).exists():
            return ImageFont.truetype(path, size)

    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _fit_text(draw: ImageDraw.ImageDraw, text: str, max_width: int, size: int, bold: bool = True, min_size: int = 24):
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


def _draw_check(draw: ImageDraw.ImageDraw, x: int, y: int):
    green = (124, 255, 0)
    _rounded_rect(draw, (x, y, x + 68, y + 68), 12, fill=green, outline=(175, 255, 120), width=2)
    draw.line((x + 17, y + 36, x + 30, y + 50), fill=(255, 255, 255), width=8)
    draw.line((x + 30, y + 50, x + 53, y + 19), fill=(255, 255, 255), width=8)


def _draw_clock(draw: ImageDraw.ImageDraw, x: int, y: int):
    green = (124, 255, 0)
    draw.ellipse((x, y, x + 62, y + 62), outline=green, width=5)
    draw.line((x + 31, y + 13, x + 31, y + 35), fill=green, width=4)
    draw.line((x + 31, y + 35, x + 48, y + 46), fill=green, width=4)


def _generate_pick_card(row: dict) -> Path:
    league = _get_value(row, "LEAGUE", fallback="TT Elite")
    est = _get_value(row, "EST")
    player_1 = _get_value(row, "Player 1", "Player1", fallback="TBD")
    player_2 = _get_value(row, "Player 2", "Player2", fallback="TBD")
    bet = _get_value(row, "BET", fallback="No Bet Found")
    unit = _get_value(row, "Unit", "Units", fallback="1")
    history = _get_value(row, "History", "Unit History", fallback="N/A")

    width, height = 1200, 1320
    green = (124, 255, 0)
    white = (245, 245, 245)
    muted = (185, 195, 195)
    dark = (5, 8, 9)

    img = Image.new("RGBA", (width, height), (*dark, 255))
    draw = ImageDraw.Draw(img)

    # Subtle background texture.
    for x in range(0, width, 22):
        for y in range(0, height, 22):
            draw.ellipse((x, y, x + 2, y + 2), fill=(25, 42, 36, 80))

    # Outer card border and left accent.
    _rounded_rect(draw, (18, 16, width - 18, height - 16), 18, fill=(8, 12, 13), outline=(46, 58, 58), width=2)
    draw.rectangle((18, 16, 26, height - 16), fill=green)

    # Brand row. No emojis here because Linux fonts render many emojis as boxes.
    _paste_contain(img, AVATAR_PATH, (62, 42, 122, 102))
    draw.rectangle((150, 62, 178, 90), outline=green, width=3)
    draw.rectangle((156, 68, 172, 84), fill=green)
    draw.text((208, 58), BRAND_NAME, font=_font(38, True), fill=white)
    _paste_contain(img, AVATAR_PATH, (945, 38, 1132, 224))

    # Top time and matchup bar. Logo has its own space so text cannot run underneath it.
    bar = (58, 122, 910, 246)
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(bar, radius=22, outline=green, width=7)
    glow = glow.filter(ImageFilter.GaussianBlur(7))
    img.alpha_composite(glow)
    _rounded_rect(draw, bar, 22, fill=(8, 13, 14), outline=green, width=3)

    _draw_clock(draw, 88, 154)
    time_text, time_font = _fit_text(draw, est, 190, 48, True, 34)
    draw.text((178, 147), time_text, font=time_font, fill=white)
    draw.text((188, 203), "EST", font=_font(30, True), fill=green)
    draw.line((368, 145, 368, 222), fill=(125, 138, 138), width=2)

    matchup = f"{player_1} vs {player_2}"
    matchup_text, matchup_font = _fit_text(draw, matchup, 485, 39, True, 27)
    # Draw player names in white and a green vs only when it fits cleanly.
    if " vs " in matchup_text and not matchup_text.endswith("..."):
        p1, p2 = matchup_text.split(" vs ", 1)
        p1_w = _text_width(draw, p1 + " ", matchup_font)
        vs_w = _text_width(draw, "vs ", matchup_font)
        draw.text((415, 166), p1 + " ", font=matchup_font, fill=white)
        draw.text((415 + p1_w, 166), "vs ", font=matchup_font, fill=green)
        draw.text((415 + p1_w + vs_w, 166), p2, font=matchup_font, fill=white)
    else:
        draw.text((415, 166), matchup_text, font=matchup_font, fill=white)

    # Main play panel.
    panel_box = (58, 292, 1142, 900)
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(panel_box, radius=28, outline=green, width=8)
    glow = glow.filter(ImageFilter.GaussianBlur(10))
    img.alpha_composite(glow)
    _rounded_rect(draw, panel_box, 28, fill=(6, 12, 13), outline=green, width=3)

    # Flag and league.
    draw.rectangle((118, 340, 218, 415), fill=(235, 235, 235))
    draw.rectangle((118, 378, 218, 415), fill=(235, 25, 45))
    league_text, league_font = _fit_text(draw, league.upper(), 680, 52, True, 34)
    draw.text((260, 354), league_text, font=league_font, fill=white)
    draw.line((250, 430, 772, 430), fill=green, width=4)
    draw.line((772, 430, 832, 390), fill=green, width=4)

    draw.text((118, 460), "1 play", font=_font(36, True), fill=green)

    play_label = bet
    if history != "N/A":
        play_label = f"{bet}  •  {history} L20"

    def play_row(y: int, label: str):
        _rounded_rect(draw, (112, y, 1072, y + 106), 16, fill=(10, 15, 16), outline=(48, 60, 60), width=2)
        _draw_check(draw, 142, y + 19)
        fitted_label, fitted_font = _fit_text(draw, label, 760, 42, True, 30)
        draw.text((260, y + 32), fitted_label, font=fitted_font, fill=white)

    # Three repeated rows for the current card look. Later we can map multiple sheet rows here.
    play_row(530, play_label)
    play_row(646, play_label)
    play_row(762, play_label)

    unit_text = unit if str(unit).lower().endswith("u") else f"{unit}u"
    _rounded_rect(draw, (102, 878, 212, 922), 6, fill=(11, 20, 16), outline=green, width=2)
    draw.text((126, 882), unit_text, font=_font(30, True), fill=green)

    # Banner and footer.
    _paste_contain(img, BANNER_PATH, (58, 925, 1142, 1240))
    draw.line((118, 1246, 1082, 1246), fill=(42, 55, 55), width=1)
    _paste_contain(img, AVATAR_PATH, (60, 1252, 104, 1296))
    draw.rectangle((132, 1264, 150, 1282), outline=green, width=2)
    draw.rectangle((137, 1269, 145, 1277), fill=green)
    draw.text((176, 1258), BRAND_NAME, font=_font(28, True), fill=white)

    img = img.convert("RGB")
    img.save(GENERATED_CARD_PATH, quality=95)
    return GENERATED_CARD_PATH


def _build_embed_payload(card_file_name: str, bet: str, avatar_file_name: str | None = None) -> dict:
    embed = {
        "color": DISCORD_EMBED_COLOR,
        "title": f"📢 {bet}",
        "image": {"url": f"attachment://{card_file_name}"},
        "footer": {"text": BRAND_NAME},
    }

    if avatar_file_name:
        avatar_url = f"attachment://{avatar_file_name}"
        embed["author"] = {"name": BRAND_NAME, "icon_url": avatar_url}
        embed["thumbnail"] = {"url": avatar_url}
        embed["footer"]["icon_url"] = avatar_url

    return {
        "content": "",
        "embeds": [embed],
    }


def _post_card_to_discord(webhook_url: str, card_path: Path, bet: str) -> requests.Response:
    avatar_file_name = AVATAR_PATH.name if AVATAR_PATH.exists() else None
    payload = _build_embed_payload(card_path.name, bet, avatar_file_name)

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

        return requests.post(
            webhook_url,
            data={"payload_json": json.dumps(payload)},
            files=files,
            timeout=30,
        )
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

        bet = _get_value(row, "BET", fallback="New Bet Alert")
        print(f"✅ Found data for: {row.get('Player 1')} vs {row.get('Player 2')}")

        card_path = _generate_pick_card(row)
        response = _post_card_to_discord(webhook_url, card_path, bet)

        if response.status_code in (200, 204):
            print("🚀 Success! Visual play card posted to Discord.")
        else:
            print(f"❌ Failed. Status: {response.status_code}, Response: {response.text}")

    except Exception as e:
        print(f"❌ Python Error: {e}")


if __name__ == "__main__":
    run_automation()
