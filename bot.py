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

# Google Sheet tabs to scan. Keep your original totals card data in the first tab,
# and create a separate worksheet/tab named MONEYLINES for ML plays.
TOTALS_WORKSHEET_NAME = os.getenv("TOTALS_WORKSHEET_NAME", "")  # blank = first worksheet
MONEYLINES_WORKSHEET_NAME = os.getenv("MONEYLINES_WORKSHEET_NAME", "Moneylines")
LIVE_WORKSHEET_NAME = os.getenv("LIVE_WORKSHEET_NAME", "Live Plays")

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
    est_text = _get_value(row, "EST", fallback="").strip().upper()

    # Live tab can use EST = LIVE or NOW to post on the next bot run.
    if est_text in {"LIVE", "NOW"}:
        return True, "Live play marked for immediate posting"

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


def _iter_numbered_values(normalized: dict, base_names: tuple[str, ...], max_items: int = 10) -> list[str]:
    """Return values from duplicated/numbered sheet headers like BET, BET 2, BET 3."""
    values = []

    for index in range(1, max_items + 1):
        for base_name in base_names:
            key = base_name.strip().lower() if index == 1 else f"{base_name.strip().lower()} {index}"
            value = str(normalized.get(key, "") or "").strip()
            if value:
                values.append(value)
                break

    return values


def _get_numbered_value(normalized: dict, index: int, *base_names: str) -> str:
    for base_name in base_names:
        key = base_name.strip().lower() if index == 1 else f"{base_name.strip().lower()} {index}"
        value = str(normalized.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _detect_market_type(row: dict, plays: list[dict] | None = None) -> str:
    normalized = _normalize_row(row)
    explicit_market = " ".join(
        str(normalized.get(key, "") or "").strip().lower()
        for key in ("market", "market type", "bet type", "type")
    )

    play_text = " ".join(str(play.get("bet", "") or "").lower() for play in (plays or []))
    combined = f"{explicit_market} {play_text}"

    if any(token in combined for token in ("moneyline", "money line", " ml", "ml ")):
        return "moneyline"

    if any(token in combined for token in ("over", "under", "total", "totals")):
        return "totals"

    # If a play includes American odds and no total language, treat it as a moneyline.
    if re.search(r"(^|\s)[+-]\d{3,4}(\s|$)", combined):
        return "moneyline"

    return "totals"


def _format_moneyline_bet(bet: str, row: dict, index: int) -> str:
    """Clean up moneyline text for the card without forcing a rigid sheet format."""
    normalized = _normalize_row(row)
    raw = str(bet or "").strip()
    selection = _get_numbered_value(normalized, index, "selection", "pick", "player", "winner", "moneyline")
    odds = _get_numbered_value(normalized, index, "odds", "price", "line")

    # If the sheet has separate Selection + Odds columns, build the clean display from them.
    if selection and odds and raw.lower() in {selection.lower(), odds.lower()}:
        return f"{selection} ML {odds}"

    if selection and odds and not raw:
        return f"{selection} ML {odds}"

    if raw and "moneyline" in raw.lower():
        raw = re.sub(r"money\s*line", "ML", raw, flags=re.IGNORECASE)

    # Add ML when odds are present but the bet text does not already identify the market.
    if raw and re.search(r"(^|\s)[+-]\d{3,4}(\s|$)", raw) and not re.search(r"\bML\b", raw, re.IGNORECASE):
        raw = re.sub(r"\s+", " ", raw).strip()
        odds_match = re.search(r"[+-]\d{3,4}", raw)
        if odds_match:
            odds_text = odds_match.group(0)
            name = raw.replace(odds_text, "").strip(" -•|@")
            raw = f"{name} ML {odds_text}" if name else f"ML {odds_text}"

    return raw or "No Moneyline Found"


def _format_live_bet(bet: str, row: dict, index: int) -> tuple[str, str]:
    """Build the live-play card text from BET + optional scenario columns."""
    normalized = _normalize_row(row)
    bet = str(bet or "").strip()

    scenario = _get_numbered_value(
        normalized,
        index,
        "scenario",
        "condition",
        "if",
        "live scenario",
        "trigger",
    )

    if bet and not bet.lower().startswith("bet:"):
        bet = f"Bet: {bet}"

    return bet or "No Live Bet Found", scenario


def _collect_plays(row: dict, forced_market_type: str | None = None) -> list[dict]:
    normalized = _normalize_row(row)

    # Totals and Live Plays use BET, BET 2, BET 3 and skip blanks.
    # Moneylines uses BET by default, then checks BET 2 / BET 3 / BET 4 and skips blanks.
    initial_market_type = forced_market_type or _detect_market_type(row)
    if initial_market_type in ("totals", "live"):
        max_bets = 3
    elif initial_market_type == "moneyline":
        max_bets = 4
    else:
        max_bets = 10

    bet_values = _iter_numbered_values(normalized, ("bet", "play", "pick", "selection"), max_items=max_bets)
    market_type = forced_market_type or _detect_market_type(row, [{"bet": value} for value in bet_values])

    # Safety clamp in case market detection changes after reading the bet text.
    if market_type in ("totals", "live"):
        bet_values = bet_values[:3]
    elif market_type == "moneyline":
        bet_values = bet_values[:4]

    if not bet_values:
        default_unit = _get_numbered_value(normalized, 1, "unit", "units", "stake")
        return [{"bet": "No Bet Found", "history": "", "unit": default_unit, "market_type": market_type, "scenario": ""}]

    plays = []
    for index, bet in enumerate(bet_values, start=1):
        # History is only used on totals cards. Moneylines and live plays ignore it
        # so those cards stay clean while MATCH ID remains available for recaps.
        history = "" if market_type in ("moneyline", "live") else _get_numbered_value(normalized, index, "history", "unit history", "record")
        unit = _get_numbered_value(normalized, index, "unit", "units", "stake")
        odds = _get_numbered_value(normalized, index, "odds", "price", "line")
        scenario = ""

        if market_type == "moneyline":
            bet = _format_moneyline_bet(bet, row, index)
        elif market_type == "live":
            bet, scenario = _format_live_bet(bet, row, index)

        plays.append(
            {
                "bet": bet,
                "history": history,
                "unit": unit,
                "odds": odds,
                "market_type": market_type,
                "scenario": scenario,
            }
        )

    return plays


def _play_label(play: dict) -> str:
    label = str(play.get("bet", "") or "No Bet Found").strip()
    history = str(play.get("history", "") or "").strip()

    if history and play.get("market_type") != "moneyline":
        label += f"  •  {history} L20"

    return label


def _format_unit(unit: str) -> str:
    unit = str(unit or "").strip()
    if not unit:
        return ""
    unit = unit.upper() if unit.lower().endswith("u") else f"{unit}U"
    return unit


def _draw_market_banner(img: Image.Image, banner_frame: tuple[int, int, int, int], market_type: str):
    draw = ImageDraw.Draw(img)
    green = (124, 255, 0)
    white = (246, 247, 248)
    dark = (7, 13, 15)
    muted = (26, 43, 45)

    _rounded_rect(draw, banner_frame, 22, fill=(11, 17, 20), outline=(55, 73, 80), width=1)
    inner = (banner_frame[0] + 10, banner_frame[1] + 10, banner_frame[2] - 10, banner_frame[3] - 10)
    _rounded_rect(draw, inner, 18, fill=(238, 242, 239), outline=(35, 55, 58), width=1)

    # Side panels keep the card sports-betting themed without hardcoding the old TOTALS art.
    left_panel = (inner[0], inner[1], inner[0] + 230, inner[3])
    right_panel = (inner[2] - 230, inner[1], inner[2], inner[3])
    draw.rectangle(left_panel, fill=(8, 20, 15))
    draw.rectangle(right_panel, fill=(8, 20, 15))

    for offset in range(0, 190, 22):
        draw.line((inner[0] + offset, inner[3], inner[0] + offset + 130, inner[1]), fill=(0, 90, 34), width=4)
        draw.line((inner[2] - offset, inner[3], inner[2] - offset - 130, inner[1]), fill=(0, 90, 34), width=4)

    # Center title block.
    center_x1 = inner[0] + 210
    center_x2 = inner[2] - 210
    draw.rectangle((center_x1, inner[1], center_x2, inner[3]), fill=(239, 243, 240))

    title = "BALI BETS"
    if market_type == "moneyline":
        subtitle = "TABLE TENNIS MONEYLINES"
        badge = "MONEYLINE CARD"
    elif market_type == "live":
        subtitle = "TABLE TENNIS LIVE PLAYS"
        badge = "LIVE CARD"
    else:
        subtitle = "TABLE TENNIS TOTALS"
        badge = "TOTALS CARD"

    title_text, title_font = _fit_text(draw, title, center_x2 - center_x1 - 40, 82, True, 44)
    title_w = _text_width(draw, title_text, title_font)
    draw.text(((center_x1 + center_x2 - title_w) / 2, inner[1] + 68), title_text, font=title_font, fill=(0, 67, 35))

    sub_text, sub_font = _fit_text(draw, subtitle, center_x2 - center_x1 - 40, 42, True, 24)
    sub_w = _text_width(draw, sub_text, sub_font)
    sub_y = inner[1] + 164
    draw.text(((center_x1 + center_x2 - sub_w) / 2, sub_y), sub_text, font=sub_font, fill=dark)

    line_y = sub_y + 58
    draw.line((center_x1 + 30, line_y, center_x1 + 190, line_y), fill=green, width=5)
    draw.line((center_x2 - 190, line_y, center_x2 - 30, line_y), fill=green, width=5)

    badge_font = _font(18, True)
    badge_w = _text_width(draw, badge, badge_font)
    draw.text(((center_x1 + center_x2 - badge_w) / 2, inner[3] - 46), badge, font=badge_font, fill=(38, 51, 53))

    # Simple paddle/ball shapes, drawn in code so the file does not need another asset.
    for side in ("left", "right"):
        if side == "left":
            cx, cy = inner[0] + 108, inner[1] + 155
            handle = (cx + 34, cy + 58, cx + 72, cy + 126)
            ball = (inner[0] + 86, inner[3] - 74, inner[0] + 130, inner[3] - 30)
            if market_type == "moneyline":
                paddle_fill = (0, 76, 38)
            elif market_type == "live":
                paddle_fill = (124, 255, 0)
            else:
                paddle_fill = (202, 18, 25)
        else:
            cx, cy = inner[2] - 108, inner[1] + 155
            handle = (cx - 72, cy + 58, cx - 34, cy + 126)
            ball = (inner[2] - 130, inner[3] - 74, inner[2] - 86, inner[3] - 30)
            paddle_fill = (22, 24, 25)

        draw.ellipse((cx - 72, cy - 72, cx + 72, cy + 72), fill=paddle_fill, outline=(18, 18, 18), width=3)
        draw.rounded_rectangle(handle, radius=10, fill=(111, 62, 28), outline=(36, 24, 18), width=2)
        draw.ellipse(ball, fill=(235, 239, 236), outline=(175, 185, 180), width=2)

    # Subtle bottom glow.
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rectangle((inner[0], inner[3] - 52, inner[2], inner[3]), fill=(0, 120, 43, 42))
    glow = glow.filter(ImageFilter.GaussianBlur(18))
    img.alpha_composite(glow)


def _generate_pick_card(row: dict, forced_market_type: str | None = None) -> Path:
    league = _get_value(row, "LEAGUE", fallback="TT Elite")
    est = _get_value(row, "EST")
    player_1 = _get_value(row, "Player 1", "Player1", fallback="TBD")
    player_2 = _get_value(row, "Player 2", "Player2", fallback="TBD")
    match_id = _get_value(row, "MATCH ID", "Match ID", "match_id", fallback="").strip()

    plays = _collect_plays(row, forced_market_type=forced_market_type)
    market_type = forced_market_type or _detect_market_type(row, plays)
    for play in plays:
        play["market_type"] = market_type
    play_count = len(plays)
    primary_unit = _format_unit(plays[0].get("unit", "") or _get_value(row, "Unit", "Units", "Stake", fallback=""))

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
    rows_top = board_y + 142
    row_h = 86
    row_gap = 14
    rows_h = play_count * row_h + max(0, play_count - 1) * row_gap
    banner_y = rows_top + rows_h + 28
    banner_h = 330
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

    _paste_circle(img, AVATAR_PATH, (header[0] + 16, header_y + 17, header[0] + 70, header_y + 71), border=0)
    draw.text((header[0] + 84, header_y + 11), BRAND_NAME, font=_font(32, True), fill=white)
    alert_label = "LIVE BET ALERT" if market_type == "live" else "BET ALERT"
    alert_x = header[0] + 86
    draw.text((alert_x, header_y + 50), alert_label, font=_font(17, True), fill=green)
    draw.text((alert_x + _text_width(draw, alert_label + " ", _font(17, True)), header_y + 50), "AUTO POSTED PLAY", font=_font(17, True), fill=muted)

    logo_badge = (right - 170, header_y + 10, right - 18, header_y + 82)
    _rounded_rect(draw, logo_badge, 20, fill=(12, 20, 23), outline=(44, 60, 67), width=1)
    _paste_contain(img, AVATAR_PATH, (logo_badge[0] + 28, logo_badge[1] + 9, logo_badge[2] - 28, logo_badge[3] - 9))

    # matchup bar
    matchup_box = (left + 18, matchup_y, right - 18, matchup_y + matchup_h)
    _draw_soft_glow(img, matchup_box, radius=24, color=(124, 255, 0, 58), border=4)
    _rounded_rect(draw, matchup_box, 24, fill=(8, 14, 16), outline=green, width=2)

    time_chip = (matchup_box[0] + 18, matchup_y + 14, matchup_box[0] + 226, matchup_y + 80)
    _rounded_rect(draw, time_chip, 18, fill=(13, 23, 18), outline=(72, 118, 74), width=1)
    _draw_clock(draw, time_chip[0] + 14, time_chip[1] + 10)
    clean_est = str(est).upper().replace("EST", "").replace("EDT", "").strip() or est
    time_text, time_font = _fit_text(draw, clean_est, 130, 23, True, 16)
    time_text_x = time_chip[0] + 76
    draw.text((time_text_x, time_chip[1] + 7), time_text, font=time_font, fill=white)
    draw.text((time_text_x + 2, time_chip[1] + 38), "EST", font=_font(14, True), fill=green)

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

    if match_id:
        match_chip = (count_chip[2] + 18, chip_y, count_chip[2] + 258, chip_y + chip_h)
        _rounded_rect(draw, match_chip, 16, fill=(14, 22, 26), outline=(54, 73, 80), width=1)
        match_label = f"MATCH ID {match_id}"
        match_text, match_font = _fit_text(draw, match_label, match_chip[2] - match_chip[0] - 24, 18, True, 12)
        draw.text((match_chip[0] + 12, chip_y + 11), match_text, font=match_font, fill=off_white)

    if primary_unit:
        unit_chip = (board[2] - 156, chip_y, board[2] - 22, chip_y + chip_h)
        _rounded_rect(draw, unit_chip, 16, fill=(13, 24, 17), outline=(74, 121, 78), width=1)
        unit_label = f"STAKE {primary_unit}"
        unit_w = _text_width(draw, unit_label, _font(18, True))
        draw.text((unit_chip[0] + ((unit_chip[2] - unit_chip[0]) - unit_w) / 2, chip_y + 10), unit_label, font=_font(18, True), fill=green)

    draw.line((board[0] + 22, board_y + 78, board[2] - 22, board_y + 78), fill=soft_stroke, width=1)
    if market_type == "moneyline":
        official_title = "OFFICIAL MONEYLINES"
    elif market_type == "live":
        official_title = "OFFICIAL LIVE PLAYS"
    else:
        official_title = "OFFICIAL PLAYS"
    draw.text((board[0] + 22, board_y + 98), official_title, font=_font(16, True), fill=muted)

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

        bet_text = str(play.get("bet", "") or "No Bet Found").strip()
        history_text = str(play.get("history", "") or "").strip()
        main_x = row_x1 + 134
        max_main_w = row_x2 - main_x - 70

        if history_text:
            record_label = f"{history_text} L20"
            record_font = _font(22, True)
            bullet_font = _font(22, True)

            # Moneylines should read cleanly as the pick/price, not as a totals-history formula.
            if market_type == "moneyline":
                bet_fit, bet_font = _fit_text(draw, bet_text, max_main_w, 23, True, 15)
                draw.text((main_x, current_y + 16), bet_fit, font=bet_font, fill=white)
            else:
                record_w = _text_width(draw, record_label, record_font)
                bullet_w = _text_width(draw, "  •  ", bullet_font)
                bet_fit, bet_font = _fit_text(draw, bet_text, max_main_w - record_w - bullet_w, 23, True, 15)
                draw.text((main_x, current_y + 16), bet_fit, font=bet_font, fill=white)
                bet_w = _text_width(draw, bet_fit, bet_font)
                draw.text((main_x + bet_w, current_y + 16), "  •  ", font=bullet_font, fill=off_white)
                draw.text((main_x + bet_w + bullet_w, current_y + 16), record_label, font=record_font, fill=white)
        else:
            bet_fit, bet_font = _fit_text(draw, bet_text, max_main_w, 23, True, 15)
            draw.text((main_x, current_y + 16), bet_fit, font=bet_font, fill=white)

        meta_parts = []
        scenario_text = str(play.get("scenario", "") or "").strip()
        if scenario_text:
            meta_parts.append(f"If {scenario_text}" if not scenario_text.lower().startswith("if ") else scenario_text)
        if play.get("unit"):
            meta_parts.append(_format_unit(play.get("unit", "")))
        if history_text and market_type == "totals":
            meta_parts.append("History " + history_text)
        if meta_parts:
            meta_line = "   •   ".join(meta_parts)
            meta_fit, meta_font = _fit_text(draw, meta_line, max_main_w, 15, False, 11)
            draw.text((main_x, current_y + 50), meta_fit, font=meta_font, fill=off_white)

        draw.rounded_rectangle((row_x2 - 12, current_y + 16, row_x2 - 7, current_y + row_h - 16), radius=3, fill=(110, 240, 0))
        current_y += row_h + row_gap

    # banner
    banner_frame = (board[0] + 22, banner_y, board[2] - 22, banner_y + banner_h)
    if market_type in ("moneyline", "live"):
        _draw_market_banner(img, banner_frame, market_type)
    else:
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



def _worksheet_jobs(client, sheet_id: str):
    """Return worksheet + forced market type jobs.

    Totals keeps using the first tab by default.
    Moneylines uses its own Google Sheet tab named by MONEYLINES_WORKSHEET_NAME.
    Live plays uses its own Google Sheet tab named by LIVE_WORKSHEET_NAME.
    """
    spreadsheet = client.open_by_key(sheet_id)
    jobs = []

    try:
        totals_sheet = spreadsheet.worksheet(TOTALS_WORKSHEET_NAME) if TOTALS_WORKSHEET_NAME else spreadsheet.sheet1
        jobs.append((totals_sheet, "totals"))
    except Exception as exc:
        print(f"⚠️ Totals tab not found: {TOTALS_WORKSHEET_NAME or 'first worksheet'} | {exc}")

    try:
        moneylines_sheet = spreadsheet.worksheet(MONEYLINES_WORKSHEET_NAME)
        jobs.append((moneylines_sheet, "moneyline"))
    except Exception as exc:
        print(f"⚠️ Moneylines tab not found: {MONEYLINES_WORKSHEET_NAME}. Create a Google Sheet tab named '{MONEYLINES_WORKSHEET_NAME}'. | {exc}")

    try:
        live_sheet = spreadsheet.worksheet(LIVE_WORKSHEET_NAME)
        jobs.append((live_sheet, "live"))
    except Exception as exc:
        print(f"⚠️ Live Plays tab not found: {LIVE_WORKSHEET_NAME}. Create a Google Sheet tab named '{LIVE_WORKSHEET_NAME}'. | {exc}")

    return jobs


def _run_sheet_tab(sheet, forced_market_type: str) -> int:
    tab_name = getattr(sheet, "title", forced_market_type)
    rows, row_numbers, header_map = _rows_from_sheet(sheet)

    if not rows:
        print(f"⚠️ {tab_name}: Sheet tab is empty.")
        return 0

    posted_col = _ensure_posted_column(sheet, header_map)
    eligible_rows = []

    for row, row_number in zip(rows, row_numbers):
        posted_value = _get_value(row, "POSTED", fallback="").strip()

        if posted_value:
            print(f"{tab_name} row {row_number}: Already posted. Skipping.")
            continue

        should_post, reason = _is_post_time(row)
        print(f"{tab_name} row {row_number}: {reason}")

        if should_post:
            play_time = _parse_est_datetime(row)
            eligible_rows.append((play_time, row_number, row))

    if not eligible_rows:
        print(f"ℹ️ {tab_name}: No eligible plays to post right now.")
        return 0

    eligible_rows.sort(key=lambda item: item[0] or datetime.min.replace(tzinfo=EST_TZ))
    posted_count = 0

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

    for play_time, row_number, row in eligible_rows[:MAX_POSTS_PER_RUN]:
        match_id = _get_value(row, "MATCH ID", "Match ID", fallback="").strip()
        match_id_log = f" | MATCH ID {match_id}" if match_id else ""
        print(f"✅ {tab_name}: Posting {forced_market_type} play for row {row_number}: {row.get('Player 1')} vs {row.get('Player 2')}{match_id_log}")

        card_path = _generate_pick_card(row, forced_market_type=forced_market_type)
        response = _post_card_to_discord(webhook_url, card_path)

        if response.status_code in (200, 204):
            _mark_posted(sheet, row_number, posted_col)
            posted_count += 1
            print(f"🚀 {tab_name}: Success! Row {row_number} posted and marked POSTED.")
        else:
            print(f"❌ {tab_name}: Failed row {row_number}. Status: {response.status_code}, Response: {response.text}")

    return posted_count


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
        jobs = _worksheet_jobs(client, sheet_id)

        if not jobs:
            print("❌ No usable worksheet tabs found. Add your totals tab, Moneylines tab, and Live Plays tab.")
            return

        total_posted = 0
        for sheet, forced_market_type in jobs:
            total_posted += _run_sheet_tab(sheet, forced_market_type)

        print(f"✅ Finished. Posted {total_posted} play(s) across all tabs.")

    except Exception as e:
        print(f"❌ Python Error: {e}")


if __name__ == "__main__":
    run_automation()
