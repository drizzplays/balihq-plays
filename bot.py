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
DEFAULT_SHEET_ID = "YOUR_SHEET_ID_HERE"


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


def _font(size: int, bold: bool = False):
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in font_paths:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    size: int,
    bold: bool = True,
    min_size: int = 24,
):
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
    _rounded_rect(
        draw,
        (x, y, x + 68, y + 68),
        12,
        fill=green,
        outline=(175, 255, 120),
        width=2,
    )
    draw.line((x + 17, y + 36, x + 30, y + 50), fill=(255, 255, 255), width=8)
    draw.line((x + 30, y + 50, x + 53, y + 19), fill=(255, 255, 255), width=8)


def _draw_clock(draw: ImageDraw.ImageDraw, x: int, y: int):
    green = (124, 255, 0)
    draw.ellipse((x, y, x + 62, y + 62), outline=green, width=5)
    draw.line((x + 31, y + 13, x + 31, y + 35), fill=green, width=4)
    draw.line((x + 31, y + 35, x + 48, y + 46), fill=green, width=4)


def _collect_plays(row: dict) -> list[dict]:
    normalized = _normalize_row(row)
    plays: list[dict] = []

    def get_any(*names: str) -> str:
        for name in names:
            value = str(normalized.get(name.strip().lower(), "") or "").strip()
            if value:
                return value
        return ""

    # First play
    first_bet = get_any("bet")
    if first_bet:
        plays.append(
            {
                "bet": first_bet,
                "history": get_any("history", "unit history"),
                "unit": get_any("unit", "units"),
            }
        )

    # Additional plays if you have BET 2 / HISTORY 2 / UNIT 2, etc.
    for i in range(2, 8):
        bet = get_any(f"bet {i}", f"bet{i}", f"play {i}", f"play{i}")
        if not bet:
            continue

        plays.append(
            {
                "bet": bet,
                "history": get_any(
                    f"history {i}",
                    f"history{i}",
                    f"unit history {i}",
                    f"unit history{i}",
                ),
                "unit": get_any(
                    f"unit {i}",
                    f"unit{i}",
                    f"units {i}",
                    f"units{i}",
                ),
            }
        )

    if not plays:
        plays.append(
            {
                "bet": "No Bet Found",
                "history": "",
                "unit": get_any("unit", "units"),
            }
        )

    return plays


def _play_label(play: dict) -> str:
    label = play.get("bet", "") or "No Bet Found"
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
    primary_unit = _format_unit(
        plays[0].get("unit", "") or _get_value(row, "Unit", "Units", fallback="")
    )

    width, height = 1200, 1320
    green = (124, 255, 0)
    white = (245, 245, 245)
    dark = (5, 8, 9)

    img = Image.new("RGBA", (width, height), (*dark, 255))
    draw = ImageDraw.Draw(img)

    # background texture
    for x in range(0, width, 22):
        for y in range(0, height, 22):
            draw.ellipse((x, y, x + 2, y + 2), fill=(25, 42, 36, 80))

    # outer card
    _rounded_rect(
        draw,
        (18, 16, width - 18, height - 16),
        18,
        fill=(8, 12, 13),
        outline=(46, 58, 58),
        width=2,
    )
    draw.rectangle((18, 16, 26, height - 16), fill=green)

    # brand row - no big blocking logo
    _paste_contain(img, AVATAR_PATH, (62, 42, 122, 102))
    draw.text((148, 58), BRAND_NAME, font=_font(38, True), fill=white)

    # top bar
    bar = (58, 122, 1142, 246)
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(bar, radius=22, outline=green, width=7)
    glow = glow.filter(ImageFilter.GaussianBlur(7))
    img.alpha_composite(glow)

    _rounded_rect(draw, bar, 22, fill=(8, 13, 14), outline=green, width=3)

    _draw_clock(draw, 88, 154)
    time_text, time_font = _fit_text(draw, est, 210, 48, True, 34)
    draw.text((178, 147), time_text, font=time_font, fill=white)
    draw.text((188, 203), "EST", font=_font(30, True), fill=green)
    draw.line((372, 145, 372, 222), fill=(125, 138, 138), width=2)

    matchup = f"{player_1} vs {player_2}"
    matchup_text, matchup_font = _fit_text(draw, matchup, 700, 39, True, 27)

    if " vs " in matchup_text and not matchup_text.endswith("..."):
        p1, p2 = matchup_text.split(" vs ", 1)
        p1_w = _text_width(draw, p1 + " ", matchup_font)
        vs_w = _text_width(draw, "vs ", matchup_font)

        draw.text((420, 166), p1 + " ", font=matchup_font, fill=white)
        draw.text((420 + p1_w, 166), "vs ", font=matchup_font, fill=green)
        draw.text((420 + p1_w + vs_w, 166), p2, font=matchup_font, fill=white)
    else:
        draw.text((420, 166), matchup_text, font=matchup_font, fill=white)

    # dynamic play panel
    panel_top = 292
    rows_top = 530
    row_height = 106
    row_gap = 10
    rows_bottom = rows_top + play_count * (row_height + row_gap) - row_gap
    unit_box_top = rows_bottom + 14
    panel_bottom = unit_box_top + 44 + 10

    panel_box = (58, panel_top, 1142, panel_bottom)
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(panel_box, radius=28, outline=green, width=8)
    glow = glow.filter(ImageFilter.GaussianBlur(10))
    img.alpha_composite(glow)

    _rounded_rect(draw, panel_box, 28, fill=(6, 12, 13), outline=green, width=3)

    # league
    draw.rectangle((118, 340, 218, 415), fill=(235, 235, 235))
    draw.rectangle((118, 378, 218, 415), fill=(235, 25, 45))

    league_text, league_font = _fit_text(draw, league.upper(), 680, 52, True, 34)
    draw.text((260, 354), league_text, font=league_font, fill=white)
    draw.line((250, 430, 772, 430), fill=green, width=4)
    draw.line((772, 430, 832, 390), fill=green, width=4)

    play_word = "play" if play_count == 1 else "plays"
    draw.text((118, 460), f"{play_count} {play_word}", font=_font(36, True), fill=green)

    def play_row(y: int, label: str):
        _rounded_rect(
            draw,
            (112, y, 1072, y + row_height),
            16,
            fill=(10, 15, 16),
            outline=(48, 60, 60),
            width=2,
        )
        _draw_check(draw, 142, y + 19)
        fitted_label, fitted_font = _fit_text(draw, label, 760, 42, True, 30)
        draw.text((260, y + 32), fitted_label, font=fitted_font, fill=white)

    current_y = rows_top
    for play in plays:
        play_row(current_y, _play_label(play))
        current_y += row_height + row_gap

    if primary_unit:
        _rounded_rect(
            draw,
            (102, unit_box_top, 212, unit_box_top + 44),
            6,
            fill=(11, 20, 16),
            outline=green,
            width=2,
        )
        draw.text((124, unit_box_top + 4), primary_unit, font=_font(30, True), fill=green)

    banner_top = panel_bottom + 28
    _paste_contain(img, BANNER_PATH, (58, banner_top, 1142, banner_top + 300))

    footer_y = height - 68
    draw.line((118, footer_y - 6, 1082, footer_y - 6), fill=(42, 55, 55), width=1)
    _paste_contain(img, AVATAR_PATH, (60, footer_y, 104, footer_y + 44))
    draw.text((122, footer_y + 4), BRAND_NAME, font=_font(28, True), fill=white)

    img = img.convert("RGB")
    img.save(GENERATED_CARD_PATH, quality=95)
    return GENERATED_CARD_PATH


def _build_embed_payload(card_file_name: str, avatar_file_name: str | None = None) -> dict:
    embed = {
        "color": DISCORD_EMBED_COLOR,
        "title": "📢 BET ALERT",
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
