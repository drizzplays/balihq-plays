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
    from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
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

# Posts starting 5 minutes before the listed EST game time.
# The 3-minute late grace prevents GitHub Actions delays from missing a play.
# MAX_POSTS_PER_RUN keeps the bot from dumping multiple eligible rows at once.
POST_WINDOW_MINUTES = 5
POST_LATE_GRACE_MINUTES = 3
MAX_POSTS_PER_RUN = 1


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
    post_start = play_time - timedelta(minutes=POST_WINDOW_MINUTES)
    post_end = play_time + timedelta(minutes=POST_LATE_GRACE_MINUTES)

    if post_start <= now <= post_end:
        return True, (
            f"Inside EST post window: "
            f"{post_start.strftime('%I:%M %p')} - {post_end.strftime('%I:%M %p')} EST"
        )

    return False, (
        f"Not time yet. "
        f"Now: {now.strftime('%I:%M %p')} EST | "
        f"Post window: {post_start.strftime('%I:%M %p')} - {post_end.strftime('%I:%M %p')} EST"
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


def _paste_cover(canvas: Image.Image, image_path: Path, box: tuple[int, int, int, int], radius: int = 0):
    if not image_path.exists():
        return

    img = Image.open(image_path).convert("RGBA")
    target_w = box[2] - box[0]
    target_h = box[3] - box[1]
    img = ImageOps.fit(img, (target_w, target_h), method=Image.LANCZOS)

    if radius > 0:
        mask = Image.new("L", (target_w, target_h), 0)
        md = ImageDraw.Draw(mask)
        md.rounded_rectangle((0, 0, target_w, target_h), radius=radius, fill=255)
        card = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
        card.paste(img, (0, 0), mask)
        img = card

    canvas.alpha_composite(img, (box[0], box[1]))


def _paste_circle(canvas: Image.Image, image_path: Path, box: tuple[int, int, int, int], border_color=(124, 255, 0), border=4):
    if not image_path.exists():
        return

    target_w = box[2] - box[0]
    target_h = box[3] - box[1]
    size = min(target_w, target_h)

    img = Image.open(image_path).convert("RGBA")
    img = ImageOps.fit(img, (size, size), method=Image.LANCZOS)

    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse((0, 0, size - 1, size - 1), fill=255)

    circ = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    circ.paste(img, (0, 0), mask)

    if border > 0:
        bd = ImageDraw.Draw(circ)
        bd.ellipse((border // 2, border // 2, size - 1 - border // 2, size - 1 - border // 2), outline=border_color, width=border)

    x = box[0] + (target_w - size) // 2
    y = box[1] + (target_h - size) // 2
    canvas.alpha_composite(circ, (x, y))


def _draw_soft_glow(base: Image.Image, box, radius: int, color=(124, 255, 0, 100), border=4):
    glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(box, radius=radius, outline=color, width=border)
    glow = glow.filter(ImageFilter.GaussianBlur(10))
    base.alpha_composite(glow)


def _draw_check(draw: ImageDraw.ImageDraw, x: int, y: int):
    green = (124, 255, 0)
    _rounded_rect(draw, (x, y, x + 48, y + 48), 14, fill=(14, 30, 18), outline=green, width=2)
    draw.line((x + 12, y + 26, x + 21, y + 34), fill=green, width=6)
    draw.line((x + 20, y + 34, x + 36, y + 14), fill=green, width=6)


def _draw_clock(draw: ImageDraw.ImageDraw, x: int, y: int):
    green = (124, 255, 0)
    draw.ellipse((x, y, x + 46, y + 46), outline=green, width=3)
    draw.line((x + 23, y + 9, x + 23, y + 24), fill=green, width=3)
    draw.line((x + 23, y + 24, x + 34, y + 31), fill=green, width=3)


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
    unit = unit.upper() if unit.lower().endswith("u") else f"{unit}U"
    return unit


def _generate_pick_card(row: dict) -> Path:
    league = _get_value(row, "LEAGUE", fallback="TT Elite")
    est = _get_value(row, "EST")
    player_1 = _get_value(row, "Player 1", "Player1", fallback="TBD")
    player_2 = _get_value(row, "Player 2", "Player2", fallback="TBD")

    plays = _collect_plays(row)
    play_count = len(plays)
    primary_unit = _format_unit(plays[0].get("unit", "") or _get_value(row, "Unit", "Units", fallback=""))

    width = 1200
    outer_pad = 24
    left = outer_pad + 18
    right = width - outer_pad - 18

    green = (124, 255, 0)
    green_glow = (124, 255, 0, 56)
    white = (246, 247, 248)
    off_white = (204, 210, 214)
    muted = (104, 117, 126)
    bg_top = (5, 9, 12)
    bg_bottom = (8, 16, 19)
    shell_fill = (8, 12, 15)
    panel_fill = (10, 15, 18)
    row_fill = (13, 20, 24)
    stroke = (39, 54, 60)
    soft_stroke = (28, 41, 47)

    header_y = 38
    header_h = 92
    matchup_y = header_y + header_h + 18
    matchup_h = 94
    board_y = matchup_y + matchup_h + 24

    chip_h = 42
    rows_top = board_y + 126
    row_h = 82
    row_gap = 14
    rows_h = play_count * row_h + max(0, play_count - 1) * row_gap
    banner_y = rows_top + rows_h + 26
    banner_h = 356
    board_bottom = banner_y + banner_h + 24
    total_h = board_bottom + 42

    img = Image.new("RGBA", (width, total_h), bg_top + (255,))
    draw = ImageDraw.Draw(img)

    # background
    for y in range(total_h):
        t = y / max(1, total_h - 1)
        r = int(bg_top[0] * (1 - t) + bg_bottom[0] * t)
        g = int(bg_top[1] * (1 - t) + bg_bottom[1] * t)
        b = int(bg_top[2] * (1 - t) + bg_bottom[2] * t)
        draw.line((0, y, width, y), fill=(r, g, b, 255))
    for x in range(-200, width + 220, 120):
        draw.line((x, 0, x + 250, total_h), fill=(18, 30, 34, 28), width=2)
    for x in range(0, width, 26):
        for y in range(0, total_h, 26):
            draw.ellipse((x, y, x + 2, y + 2), fill=(20, 34, 38, 66))

    shell = (outer_pad, 18, width - outer_pad, total_h - 18)
    _draw_soft_glow(img, shell, radius=30, color=(124, 255, 0, 48), border=6)
    _rounded_rect(draw, shell, 30, fill=shell_fill, outline=(46, 62, 70), width=2)
    _rounded_rect(draw, (outer_pad + 10, 28, width - outer_pad - 10, total_h - 28), 26, fill=None, outline=(15, 25, 30), width=1)

    # header
    header = (left, header_y, right, header_y + header_h)
    _rounded_rect(draw, header, 24, fill=(11, 18, 21), outline=(42, 58, 65), width=1)

    _paste_circle(img, AVATAR_PATH, (header[0] + 16, header_y + 17, header[0] + 70, header_y + 71), border_color=green, border=3)
    draw.text((header[0] + 84, header_y + 11), BRAND_NAME, font=_font(32, True), fill=white)
    draw.text((header[0] + 86, header_y + 50), "BET ALERT", font=_font(17, True), fill=green)
    draw.text((header[0] + 192, header_y + 50), "AUTO POSTED PLAY", font=_font(17, True), fill=muted)

    logo_badge = (right - 170, header_y + 10, right - 18, header_y + 82)
    # cleaner top accent line: keep it away from the avatar/text on the left and logo badge on the right
    accent_y1 = header[1] + 8
    accent_y2 = accent_y1 + 4
    accent_x1 = header[0] + 62
    accent_x2 = logo_badge[0] - 18
    draw.rounded_rectangle((accent_x1, accent_y1, accent_x2, accent_y2), radius=2, fill=(110, 240, 0, 170))
    _rounded_rect(draw, logo_badge, 20, fill=(12, 20, 23), outline=(44, 60, 67), width=1)
    _paste_contain(img, AVATAR_PATH, (logo_badge[0] + 28, logo_badge[1] + 9, logo_badge[2] - 28, logo_badge[3] - 9))

    # matchup bar
    matchup_box = (left + 18, matchup_y, right - 18, matchup_y + matchup_h)
    _draw_soft_glow(img, matchup_box, radius=24, color=(124, 255, 0, 58), border=4)
    _rounded_rect(draw, matchup_box, 24, fill=(8, 14, 16), outline=green, width=2)

    time_chip = (matchup_box[0] + 18, matchup_y + 14, matchup_box[0] + 206, matchup_y + 80)
    _rounded_rect(draw, time_chip, 18, fill=(13, 23, 18), outline=(72, 118, 74), width=1)
    _draw_clock(draw, time_chip[0] + 14, time_chip[1] + 10)
    time_text, time_font = _fit_text(draw, est, 112, 22, True, 16)
    time_text_x = time_chip[0] + 72
    draw.text((time_text_x, time_chip[1] + 9), time_text, font=time_font, fill=white)
    draw.text((time_text_x, time_chip[1] + 37), "EST", font=_font(15, True), fill=green)

    divider_x = time_chip[2] + 28
    draw.line((divider_x, matchup_y + 18, divider_x, matchup_y + matchup_h - 18), fill=(56, 74, 81), width=2)
    draw.text((divider_x + 24, matchup_y + 12), "MATCHUP", font=_font(15, True), fill=muted)

    matchup = f"{player_1} vs {player_2}"
    matchup_text, matchup_font = _fit_text(draw, matchup, matchup_box[2] - (divider_x + 24) - 28, 27, True, 17)
    tx = divider_x + 24
    ty = matchup_y + 41
    if " vs " in matchup_text and not matchup_text.endswith("..."):
        p1, p2 = matchup_text.split(" vs ", 1)
        p1_w = _text_width(draw, p1 + " ", matchup_font)
        vs_w = _text_width(draw, "vs ", matchup_font)
        draw.text((tx, ty), p1 + " ", font=matchup_font, fill=white)
        draw.text((tx + p1_w, ty), "vs ", font=matchup_font, fill=green)
        draw.text((tx + p1_w + vs_w, ty), p2, font=matchup_font, fill=white)
    else:
        draw.text((tx, ty), matchup_text, font=matchup_font, fill=white)

    # board
    board = (left + 18, board_y, right - 18, board_bottom)
    _draw_soft_glow(img, board, radius=28, color=(124, 255, 0, 56), border=5)
    _rounded_rect(draw, board, 28, fill=panel_fill, outline=(54, 72, 79), width=1)

    chip_y = board_y + 22
    league_chip = (board[0] + 22, chip_y, board[0] + 292, chip_y + chip_h)
    _rounded_rect(draw, league_chip, 16, fill=(14, 22, 26), outline=(54, 73, 80), width=1)
    league_text, league_font = _fit_text(draw, league.upper(), 220, 23, True, 15)
    draw.text((league_chip[0] + 16, chip_y + 10), league_text, font=league_font, fill=white)

    count_chip = (league_chip[2] + 18, chip_y, league_chip[2] + 176, chip_y + chip_h)
    _rounded_rect(draw, count_chip, 16, fill=(13, 24, 17), outline=(74, 121, 78), width=1)
    play_word = "PLAY" if play_count == 1 else "PLAYS"
    draw.text((count_chip[0] + 16, chip_y + 10), f"{play_count} {play_word}", font=_font(18, True), fill=green)

    if primary_unit:
        unit_chip = (board[2] - 156, chip_y, board[2] - 22, chip_y + chip_h)
        _rounded_rect(draw, unit_chip, 16, fill=(13, 24, 17), outline=(74, 121, 78), width=1)
        unit_label = f"UNIT {primary_unit}"
        unit_w = _text_width(draw, unit_label, _font(18, True))
        draw.text((unit_chip[0] + ((unit_chip[2] - unit_chip[0]) - unit_w) / 2, chip_y + 10), unit_label, font=_font(18, True), fill=green)

    draw.line((board[0] + 22, board_y + 78, board[2] - 22, board_y + 78), fill=soft_stroke, width=1)
    draw.text((board[0] + 22, board_y + 94), "OFFICIAL PLAYS", font=_font(16, True), fill=muted)

    # play rows
    row_x1 = board[0] + 22
    row_x2 = board[2] - 22
    current_y = rows_top

    for idx, play in enumerate(plays, start=1):
        row_box = (row_x1, current_y, row_x2, current_y + row_h)
        _rounded_rect(draw, row_box, 18, fill=row_fill, outline=stroke, width=1)

        num_chip = (row_x1 + 16, current_y + 20, row_x1 + 58, current_y + 62)
        _rounded_rect(draw, num_chip, 14, fill=(12, 21, 17), outline=(68, 112, 70), width=1)
        num_text = str(idx)
        num_w = _text_width(draw, num_text, _font(20, True))
        draw.text((num_chip[0] + (42 - num_w) / 2, current_y + 25), num_text, font=_font(20, True), fill=green)

        _draw_check(draw, row_x1 + 74, current_y + 17)

        label = _play_label(play)
        label_text, label_font = _fit_text(draw, label, row_x2 - (row_x1 + 136) - 60, 23, True, 15)
        draw.text((row_x1 + 134, current_y + 16), label_text, font=label_font, fill=white)

        meta_parts = []
        if play.get("unit"):
            meta_parts.append(_format_unit(play.get("unit", "")))
        if play.get("history"):
            meta_parts.append(f"History {str(play.get('history')).strip()}")
        if meta_parts:
            draw.text((row_x1 + 134, current_y + 47), "   •   ".join(meta_parts), font=_font(15, False), fill=off_white)

        draw.rounded_rectangle((row_x2 - 12, current_y + 14, row_x2 - 6, current_y + row_h - 14), radius=3, fill=green)
        current_y += row_h + row_gap

    # banner
    banner_frame = (board[0] + 22, banner_y, board[2] - 22, banner_y + banner_h)
    _rounded_rect(draw, banner_frame, 22, fill=(11, 17, 20), outline=(55, 73, 80), width=1)
    _paste_cover(img, BANNER_PATH, (banner_frame[0] + 10, banner_frame[1] + 10, banner_frame[2] - 10, banner_frame[3] - 10), radius=18)

    gloss = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(gloss)
    gd.polygon([
        (banner_frame[0] + 10, banner_frame[1] + 10),
        (banner_frame[0] + 310, banner_frame[1] + 10),
        (banner_frame[0] + 220, banner_frame[1] + 96),
        (banner_frame[0] + 10, banner_frame[1] + 96),
    ], fill=(255, 255, 255, 18))
    img.alpha_composite(gloss)

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
        eligible_rows = []

        for row, row_number in zip(rows, row_numbers):
            posted_value = _get_value(row, "POSTED", fallback="").strip()

            if posted_value:
                print(f"Row {row_number}: Already posted. Skipping.")
                continue

            should_post, reason = _is_post_time(row)
            print(f"Row {row_number}: {reason}")

            if should_post:
                play_time = _parse_est_datetime(row)
                eligible_rows.append((play_time, row_number, row))

        if not eligible_rows:
            print("ℹ️ No eligible plays to post right now.")
            return

        eligible_rows.sort(key=lambda item: item[0])
        posted_count = 0

        for play_time, row_number, row in eligible_rows[:MAX_POSTS_PER_RUN]:
            print(f"✅ Posting play for row {row_number}: {row.get('Player 1')} vs {row.get('Player 2')}")

            card_path = _generate_pick_card(row)
            response = _post_card_to_discord(webhook_url, card_path)

            if response.status_code in (200, 204):
                _mark_posted(sheet, row_number, posted_col)
                posted_count += 1
                print(f"🚀 Success! Row {row_number} posted and marked POSTED.")
            else:
                print(f"❌ Failed row {row_number}. Status: {response.status_code}, Response: {response.text}")

        print(f"✅ Finished. Posted {posted_count} play(s).")

    except Exception as e:
        print(f"❌ Python Error: {e}")


if __name__ == "__main__":
    run_automation()
