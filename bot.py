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


def _rounded_rect(draw: ImageDraw.ImageDraw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _text(draw: ImageDraw.ImageDraw, xy, text, font, fill=(245, 245, 245), anchor=None):
    draw.text(xy, text, font=font, fill=fill, anchor=anchor)


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


def _generate_pick_card(row: dict) -> Path:
    league = _get_value(row, "LEAGUE", fallback="TT Elite")
    pst = _get_value(row, "PST")
    mtn = _get_value(row, "MTN", "MST")
    est = _get_value(row, "EST")
    player_1 = _get_value(row, "Player 1", "Player1", fallback="TBD")
    player_2 = _get_value(row, "Player 2", "Player2", fallback="TBD")
    bet = _get_value(row, "BET", fallback="No Bet Found")
    unit = _get_value(row, "Unit", "Units", fallback="1")
    history = _get_value(row, "History", "Unit History", fallback="N/A")

    width, height = 1200, 1320
    green = (124, 255, 0)
    white = (245, 245, 245)
    muted = (190, 194, 196)

    img = Image.new("RGBA", (width, height), (5, 8, 9, 255))
    draw = ImageDraw.Draw(img)

    # subtle background texture
    for x in range(0, width, 22):
        for y in range(0, height, 22):
            draw.ellipse((x, y, x + 2, y + 2), fill=(25, 42, 36, 85))

    # outer card border + green strip
    _rounded_rect(draw, (18, 16, width - 18, height - 16), 18, fill=(8, 12, 13), outline=(46, 58, 58), width=2)
    draw.rectangle((18, 16, 26, height - 16), fill=green)

    # top brand
    _paste_contain(img, AVATAR_PATH, (62, 42, 122, 102))
    _text(draw, (152, 60), "🌴", _font(34, True), fill=white)
    _text(draw, (210, 60), BRAND_NAME, _font(36, True), fill=white)
    _paste_contain(img, AVATAR_PATH, (944, 36, 1134, 232))

    # time/matchup bar
    bar = (58, 120, 920, 246)
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(bar, radius=22, outline=green, width=7)
    glow = glow.filter(ImageFilter.GaussianBlur(7))
    img.alpha_composite(glow)
    _rounded_rect(draw, bar, 22, fill=(8, 13, 14), outline=green, width=3)

    draw.ellipse((88, 154, 150, 216), outline=green, width=5)
    draw.line((119, 166, 119, 190), fill=green, width=4)
    draw.line((119, 190, 136, 200), fill=green, width=4)

    _text(draw, (178, 145), est, _font(48, True), fill=white)
    _text(draw, (188, 200), "EST", _font(30, True), fill=green)
    draw.line((372, 144, 372, 222), fill=(130, 140, 140), width=2)
    _text(draw, (420, 169), f"{player_1}  ", _font(38, True), fill=white)
    _text(draw, (640, 169), "vs", _font(34, True), fill=green)
    _text(draw, (700, 169), player_2, _font(38, True), fill=white)

    # main panel
    panel_box = (58, 292, 1142, 900)
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(panel_box, radius=28, outline=green, width=8)
    glow = glow.filter(ImageFilter.GaussianBlur(10))
    img.alpha_composite(glow)
    _rounded_rect(draw, panel_box, 28, fill=(6, 12, 13), outline=green, width=3)

    # flag + league
    draw.rectangle((118, 340, 218, 415), fill=(235, 235, 235))
    draw.rectangle((118, 378, 218, 415), fill=(235, 25, 45))
    _text(draw, (260, 354), league.upper(), _font(52, True), fill=white)
    draw.line((250, 430, 772, 430), fill=green, width=4)
    draw.line((772, 430, 832, 390), fill=green, width=4)

    _text(draw, (118, 460), "1 play", _font(36, True), fill=green)

    play_label = bet
    if history != "N/A":
        play_label = f"{bet}  •  {history} L20"

    def play_row(y: int, label: str):
        _rounded_rect(draw, (112, y, 1072, y + 106), 16, fill=(10, 15, 16), outline=(48, 60, 60), width=2)
        _draw_check(draw, 142, y + 19)
        _text(draw, (260, y + 32), label, _font(42, True), fill=white)

    # Three rows for the card look. Change these later when you support multiple plays.
    play_row(530, play_label)
    play_row(646, play_label)
    play_row(762, play_label)

    unit_text = unit if str(unit).lower().endswith("u") else f"{unit}u"
    _rounded_rect(draw, (102, 878, 212, 922), 6, fill=(11, 20, 16), outline=green, width=2)
    _text(draw, (126, 882), unit_text, _font(30, True), fill=green)

    # banner
    _paste_contain(img, BANNER_PATH, (58, 925, 1142, 1240))

    # footer
    _paste_contain(img, AVATAR_PATH, (60, 1250, 104, 1294))
    _text(draw, (130, 1260), "🌴", _font(24, True), fill=white)
    _text(draw, (176, 1260), BRAND_NAME, _font(28, True), fill=white)

    img = img.convert("RGB")
    img.save(GENERATED_CARD_PATH, quality=95)
    return GENERATED_CARD_PATH


def _build_embed_payload(card_file_name: str) -> dict:
    return {
        "content": "",
        "embeds": [
            {
                "color": DISCORD_EMBED_COLOR,
                "image": {"url": f"attachment://{card_file_name}"},
            }
        ],
    }


def _post_card_to_discord(webhook_url: str, card_path: Path) -> requests.Response:
    payload = _build_embed_payload(card_path.name)

    with card_path.open("rb") as card_file:
        return requests.post(
            webhook_url,
            data={"payload_json": json.dumps(payload)},
            files={"file": (card_path.name, card_file, "image/png")},
            timeout=30,
        )


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

        card_path = _generate_pick_card(row)
        response = _post_card_to_discord(webhook_url, card_path)

        if response.status_code in (200, 204):
            print("🚀 Success! Visual play card posted to Discord.")
        else:
            print(f"❌ Failed. Status: {response.status_code}, Response: {response.text}")

    except Exception as e:
        print(f"❌ Python Error: {e}")


if __name__ == "__main__":
    run_automation()
