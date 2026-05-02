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
ROLE_ID = "1500237161335881768"

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
    min_size: int = 18,
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
        (x, y, x + 62, y + 62),
        12,
        fill=green,
        outline=(175, 255, 120),
        width=2,
    )
    draw.line((x + 15, y + 33, x + 27, y + 46), fill=(255, 255, 255), width=8)
    draw.line((x + 27, y + 46, x + 48, y + 18), fill=(255, 255, 255), width=8)


def _draw_clock(draw: ImageDraw.ImageDraw, x: int, y: int):
    green = (124, 255, 0)
    draw.ellipse((x, y, x + 56, y + 56), outline=green, width=5)
    draw.line((x + 28, y + 12, x + 28, y + 31), fill=green, width=4)
    draw.line((x + 28, y + 31, x + 43, y + 41), fill=green, width=4)


def _collect_plays(row: dict) -> list[dict]:
    normalized = _normalize_row(row)
    plays: list[dict] = []

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

    width = 1200
    green = (124, 255, 0)
    white = (245, 245, 245)
    dark = (6, 10, 11)
    card_fill = (7, 12, 13)
    muted_border = (44, 56, 56)
    panel_fill = (5, 11, 12)
    row_fill = (11, 16, 17)
    row_border = (41, 51, 52)

    # Layout
    outer_pad = 18
    inner_pad = 34

    brand_row_top = 38
    brand_row_height = 76

    top_bar_top = 118
    top_bar_height = 92

    panel_top = 270
    header_top = panel_top + 56
    play_count_y = panel_top + 178

    row_height = 84
    row_gap = 14
    rows_top = panel_top + 228
    rows_bottom = rows_top + play_count * (row_height + row_gap) - row_gap

    unit_top = rows_bottom + 12
    unit_height = 30

    banner_top = unit_top + unit_height + 26
    banner_height = 235
    bottom_pad = 32

    height = banner_top + banner_height + bottom_pad

    img = Image.new("RGBA", (width, height), (*dark, 255))
    draw = ImageDraw.Draw(img)

    # Background texture
    for x in range(0, width, 24):
        for y in range(0, height, 24):
            draw.ellipse((x, y, x + 2, y + 2), fill=(20, 36, 33, 75))

    # Outer card
    _rounded_rect(
        draw,
        (outer_pad, outer_pad, width - outer_pad, height - outer_pad),
        22,
        fill=card_fill,
        outline=muted_border,
        width=2,
    )

    # Brand row
    _paste_contain(img, AVATAR_PATH, (46, brand_row_top, 96, brand_row_top + 50))
    draw.text((112, brand_row_top + 2), BRAND_NAME, font=_font(30, True), fill=white)
    _paste_contain(img, AVATAR_PATH, (970, 28, 1098, 130))

    # Top bar
    top_bar = (42, top_bar_top, 922, top_bar_top + top_bar_height)

    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(top_bar, radius=18, outline=green, width=4)
    glow = glow.filter(ImageFilter.GaussianBlur(5))
    img.alpha_composite(glow)

    _rounded_rect(draw, top_bar, 18, fill=(8, 13, 14), outline=green, width=2)

    _draw_clock(draw, 62, top_bar_top + 18)

    time_text, time_font = _fit_text(draw, est, 150, 28, True, 22)
    draw.text((138, top_bar_top + 14), time_text, font=time_font, fill=white)
    draw.text((146, top_bar_top + 52), "EST", font=_font(18, True), fill=green)

    draw.line(
        (258, top_bar_top + 16, 258, top_bar_top + top_bar_height - 16),
        fill=(115, 128, 128),
        width=2,
    )

    matchup = f"{player_1} vs {player_2}"
    matchup_text, matchup_font = _fit_text(draw, matchup, 560, 23, True, 18)

    if " vs " in matchup_text and not matchup_text.endswith("..."):
        p1, p2 = matchup_text.split(" vs ", 1)
        p1_w = _text_width(draw, p1 + " ", matchup_font)
        vs_w = _text_width(draw, "vs ", matchup_font)

        draw.text((288, top_bar_top + 31), p1 + " ", font=matchup_font, fill=white)
        draw.text((288 + p1_w, top_bar_top + 31), "vs ", font=matchup_font, fill=green)
        draw.text((288 + p1_w + vs_w, top_bar_top + 31), p2, font=matchup_font, fill=white)
    else:
        draw.text((288, top_bar_top + 31), matchup_text, font=matchup_font, fill=white)

    # Main panel
    panel_box = (42, panel_top, width - 42, banner_top + banner_height)
    # shrink panel so banner is part of same section visually
    panel_box = (42, panel_top, width - 42, banner_top + banner_height + 8)

    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(panel_box, radius=24, outline=green, width=5)
    glow = glow.filter(ImageFilter.GaussianBlur(7))
    img.alpha_composite(glow)

    _rounded_rect(draw, panel_box, 24, fill=panel_fill, outline=green, width=2)

    # League header
    draw.rectangle((86, header_top + 2, 180, header_top + 72), fill=(235, 235, 235))
    draw.rectangle((86, header_top + 37, 180, header_top + 72), fill=(235, 25, 45))

    league_text, league_font = _fit_text(draw, league.upper(), 630, 44, True, 28)
    draw.text((212, header_top + 14), league_text, font=league_font, fill=white)

    divider_y = header_top + 98
    draw.line((208, divider_y, 655, divider_y), fill=green, width=3)
    draw.line((655, divider_y, 700, divider_y - 34), fill=green, width=3)

    play_word = "play" if play_count == 1 else "plays"
    draw.text((86, play_count_y), f"{play_count} {play_word}", font=_font(28, True), fill=green)

    # Play rows
    def play_row(y: int, label: str):
        _rounded_rect(
            draw,
            (94, y, width - 94, y + row_height),
            10,
            fill=row_fill,
            outline=row_border,
            width=1,
        )

        _draw_check(draw, 118, y + 11)

        fitted_label, fitted_font = _fit_text(draw, label, 760, 28, True, 20)
        draw.text((218, y + 24), fitted_label, font=fitted_font, fill=white)

    current_y = rows_top
    for play in plays:
        play_row(current_y, _play_label(play))
        current_y += row_height + row_gap

    if primary_unit:
        _rounded_rect(
            draw,
            (86, unit_top, 162, unit_top + unit_height),
            4,
            fill=(11, 20, 16),
            outline=green,
            width=2,
        )
        draw.text((104, unit_top + 2), primary_unit, font=_font(20, True), fill=green)

    # Banner
    _paste_contain(img, BANNER_PATH, (140, banner_top, 1060, banner_top + banner_height))

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
        "allowed_mentions": {
            "roles": [ROLE_ID],
        },
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
