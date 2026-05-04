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
FONT_DIR = BASE_DIR / "fonts"
IMAGES_DIR = BASE_DIR / "images"
AVATAR_PATH = IMAGES_DIR / "avatar.png"
BANNER_PATH = IMAGES_DIR / "banner.png"
MONEYLINE_BANNER_PATH = IMAGES_DIR / "moneyline.png"
MONEYLINES_BANNER_PATH = IMAGES_DIR / "moneylines.png"
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
    # Preferred card font. Put Lexend files in /fonts inside the project.
    # Works with Regular only, or Bold/SemiBold if you add them later.
    preferred = [
        FONT_DIR / ("Lexend-Bold.ttf" if bold else "Lexend-Regular.ttf"),
        FONT_DIR / ("Lexend-SemiBold.ttf" if bold else "Lexend-Regular.ttf"),
        FONT_DIR / "Lexend-Regular.ttf",
        FONT_DIR / "Lexend-Regular(3).ttf",
        FONT_DIR / "Lexend-Regular(4).ttf",
        Path("/fonts") / ("Lexend-Bold.ttf" if bold else "Lexend-Regular.ttf"),
        Path("/fonts") / ("Lexend-SemiBold.ttf" if bold else "Lexend-Regular.ttf"),
        Path("/fonts") / "Lexend-Regular.ttf",
        Path("/fonts") / "Lexend-Regular(3).ttf",
        Path("/fonts") / "Lexend-Regular(4).ttf",
    ]

    for path in preferred:
        if path.exists():
            return ImageFont.truetype(str(path), size)

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




def _paste_contain_rounded(canvas: Image.Image, image_path: Path, box: tuple[int, int, int, int], radius: int = 0, bg_fill=(0, 0, 0, 0)):
    if not image_path.exists():
        return

    target_w = box[2] - box[0]
    target_h = box[3] - box[1]
    layer = Image.new("RGBA", (target_w, target_h), bg_fill)

    img = Image.open(image_path).convert("RGBA")
    img.thumbnail((target_w, target_h), Image.LANCZOS)
    x = (target_w - img.width) // 2
    y = (target_h - img.height) // 2
    layer.alpha_composite(img, (x, y))

    if radius > 0:
        mask = Image.new("L", (target_w, target_h), 0)
        md = ImageDraw.Draw(mask)
        md.rounded_rectangle((0, 0, target_w - 1, target_h - 1), radius=radius, fill=255)
        clipped = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
        clipped.paste(layer, (0, 0), mask)
        layer = clipped

    canvas.alpha_composite(layer, (box[0], box[1]))


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


def _slugify_league_name(value: str) -> str:
    value = str(value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_")


def _league_icon_path(league: str) -> Path | None:
    """Return a league-specific icon from /images when available.

    Current hard map:
    - any league containing 'elite' -> images/tt_elite.png

    Fallback:
    - tries a slugified filename like images/tt_elite.png for a league named TT Elite
    """
    league_text = str(league or "").strip()
    normalized = league_text.lower()

    candidates = []
    if "elite" in normalized:
        candidates.append(IMAGES_DIR / "tt_elite.png")

    slug = _slugify_league_name(league_text)
    if slug:
        candidates.append(IMAGES_DIR / f"{slug}.png")

    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            return candidate

    return None


def _draw_league_chip(img: Image.Image, draw: ImageDraw.ImageDraw, box, league: str, text_color=(246, 247, 248)):
    _draw_glossy_panel(img, box, 16, (22, 32, 38, 255), (8, 13, 18, 255), outline=(45, 60, 68), inner_outline=(255, 255, 255, 10), gloss_alpha=32)

    icon_path = _league_icon_path(league)
    text_x = box[0] + 16
    text_max_width = (box[2] - box[0]) - 32

    if icon_path:
        chip_h = box[3] - box[1]
        icon_h = min(40, chip_h - 8)
        icon_y1 = int(box[1] + (chip_h - icon_h) / 2)
        icon_wrap = (box[0] + 16, icon_y1, box[0] + 16 + icon_h, icon_y1 + icon_h)
        _draw_glossy_panel(img, icon_wrap, 10, (30, 40, 46, 255), (14, 19, 24, 255), outline=(66, 82, 90), inner_outline=(255, 255, 255, 10), gloss_alpha=24)
        icon_box = (icon_wrap[0] + 4, icon_wrap[1] + 4, icon_wrap[2] - 4, icon_wrap[3] - 4)
        _paste_contain(img, icon_path, icon_box)
        text_x = icon_wrap[2] + 16
        text_max_width = box[2] - text_x - 18

    league_text, league_font = _fit_text(draw, str(league).upper(), text_max_width, 25, True, 19)
    _draw_text_vcenter(draw, box, league_text, league_font, text_color, x=text_x)


def _draw_soft_glow(base: Image.Image, box, radius: int, color=(124, 255, 0, 100), border=4):
    glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(box, radius=radius, outline=color, width=border)
    glow = glow.filter(ImageFilter.GaussianBlur(10))
    base.alpha_composite(glow)


def _add_panel_gloss(base: Image.Image, box, radius: int = 18, top_alpha: int = 36, bottom_alpha: int = 22):
    """Add a subtle top highlight and bottom shadow for more depth."""
    x1, y1, x2, y2 = box
    w = max(1, x2 - x1)
    h = max(1, y2 - y1)

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=(0, 0, 0, 0))

    top_h = max(10, int(h * 0.45))
    for i in range(top_h):
        a = int(top_alpha * (1 - i / max(1, top_h)))
        od.line((2, i + 2, w - 3, i + 2), fill=(255, 255, 255, a), width=1)

    bottom_h = max(12, int(h * 0.28))
    for i in range(bottom_h):
        a = int(bottom_alpha * ((i + 1) / max(1, bottom_h)))
        y = h - bottom_h + i - 1
        od.line((2, y, w - 3, y), fill=(0, 0, 0, a), width=1)

    if radius > 0:
        mask = Image.new("L", (w, h), 0)
        md = ImageDraw.Draw(mask)
        md.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
        clipped = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        clipped.paste(overlay, (0, 0), mask)
        overlay = clipped

    base.alpha_composite(overlay, (x1, y1))


def _draw_drop_shadow(base: Image.Image, box, radius: int = 20, offset=(0, 10), blur: int = 18, alpha: int = 70):
    x1, y1, x2, y2 = box
    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    ox, oy = offset
    sd.rounded_rectangle((x1 + ox, y1 + oy, x2 + ox, y2 + oy), radius=radius, fill=(0, 0, 0, alpha))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    base.alpha_composite(shadow)


def _draw_glossy_panel(base: Image.Image, box, radius: int, top_color, bottom_color, outline=(52, 68, 76), inner_outline=(255, 255, 255, 16), gloss_alpha: int = 46):
    """Premium glossy rounded panel with vertical gradient and soft shine."""
    x1, y1, x2, y2 = [int(v) for v in box]
    w = max(1, x2 - x1)
    h = max(1, y2 - y1)

    panel = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pd = ImageDraw.Draw(panel)
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)

    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top_color[0] * (1 - t) + bottom_color[0] * t)
        g = int(top_color[1] * (1 - t) + bottom_color[1] * t)
        b = int(top_color[2] * (1 - t) + bottom_color[2] * t)
        a = int(top_color[3] * (1 - t) + bottom_color[3] * t) if len(top_color) == 4 and len(bottom_color) == 4 else 255
        pd.line((0, y, w, y), fill=(r, g, b, a), width=1)

    shine = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shine)
    sd.ellipse((-w * 0.18, -h * 0.85, w * 1.08, h * 0.78), fill=(255, 255, 255, gloss_alpha))
    sd.ellipse((w * 0.50, -h * 0.55, w * 1.20, h * 0.30), fill=(255, 255, 255, int(gloss_alpha * 0.35)))
    shine = shine.filter(ImageFilter.GaussianBlur(18))
    panel.alpha_composite(shine)

    bottom_tint = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bottom_tint)
    for i in range(max(12, h // 3)):
        alpha = int(24 * ((i + 1) / max(12, h // 3)))
        y = h - max(12, h // 3) + i
        bd.line((4, y, w - 5, y), fill=(0, 0, 0, alpha), width=1)
    panel.alpha_composite(bottom_tint)

    clipped = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    clipped.paste(panel, (0, 0), mask)
    base.alpha_composite(clipped, (x1, y1))

    draw = ImageDraw.Draw(base)
    draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, outline=outline, width=1)
    if inner_outline:
        draw.rounded_rectangle((x1 + 1, y1 + 1, x2 - 1, y2 - 1), radius=max(0, radius - 1), outline=inner_outline, width=1)


def _text_height(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[3] - box[1]


def _draw_text_vcenter(draw: ImageDraw.ImageDraw, box, text: str, font, fill, x=None, y_offset: int = 0):
    if x is None:
        x = box[0]
    h = _text_height(draw, text, font)
    y = box[1] + ((box[3] - box[1]) - h) / 2 - 1 + y_offset
    draw.text((x, y), text, font=font, fill=fill)


def _unit_display(unit: str) -> str:
    value = str(unit or "").strip().upper().replace("UNITS", "").replace("UNIT", "").replace("U", "").strip()
    if not value:
        return ""

    try:
        numeric = float(value)
        if numeric.is_integer():
            value = str(int(numeric))
        else:
            value = (f"{numeric:.2f}").rstrip("0").rstrip(".")
    except ValueError:
        value = value.strip()

    return f"{value} UNIT" if value == "1" else f"{value} UNITS"


def _draw_check(draw: ImageDraw.ImageDraw, x: int, y: int):
    green = (132, 255, 55)
    _rounded_rect(draw, (x, y, x + 48, y + 48), 14, fill=(18, 29, 22), outline=(96, 160, 78), width=2)
    draw.rounded_rectangle((x + 1, y + 1, x + 47, y + 47), radius=13, outline=(255, 255, 255, 14), width=1)
    draw.line((x + 12, y + 25, x + 21, y + 34), fill=green, width=6)
    draw.line((x + 20, y + 34, x + 36, y + 15), fill=green, width=6)


def _draw_clock(draw: ImageDraw.ImageDraw, x: int, y: int):
    green = (132, 255, 55)
    draw.ellipse((x, y, x + 46, y + 46), fill=(12, 24, 18), outline=green, width=3)
    draw.ellipse((x + 2, y + 2, x + 44, y + 44), outline=(255, 255, 255, 12), width=1)
    draw.line((x + 23, y + 10, x + 23, y + 24), fill=green, width=3)
    draw.line((x + 23, y + 24, x + 33, y + 31), fill=green, width=3)


def _draw_megaphone(draw: ImageDraw.ImageDraw, x: int, y: int, color=(132, 255, 55)):
    # clean custom alert icon so we don't depend on emoji font rendering
    draw.polygon([(x + 4, y + 10), (x + 17, y + 5), (x + 17, y + 21), (x + 4, y + 16)], fill=color)
    draw.rectangle((x + 17, y + 9, x + 21, y + 17), fill=color)
    draw.line((x + 8, y + 16, x + 5, y + 22), fill=color, width=3)
    draw.arc((x + 18, y + 4, x + 27, y + 13), start=300, end=60, fill=color, width=2)
    draw.arc((x + 18, y + 8, x + 31, y + 19), start=305, end=55, fill=color, width=2)


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

    # Moneylines banner should come from the images folder, not the generated fallback art.
    if market_type == "moneyline" and MONEYLINES_BANNER_PATH.exists():
        _draw_drop_shadow(img, banner_frame, radius=22, offset=(0, 12), blur=22, alpha=74)
        _draw_glossy_panel(img, banner_frame, 22, (18, 25, 30, 255), (8, 12, 16, 255), outline=(44, 58, 66), inner_outline=(255, 255, 255, 10), gloss_alpha=20)
        _paste_cover(
            img,
            MONEYLINES_BANNER_PATH,
            (banner_frame[0] + 10, banner_frame[1] + 10, banner_frame[2] - 10, banner_frame[3] - 10),
            radius=18,
        )
        return

    green = (132, 255, 55)
    dark = (7, 13, 15)

    _rounded_rect(draw, banner_frame, 22, fill=(11, 17, 20), outline=(55, 73, 80), width=1)
    _add_panel_gloss(img, banner_frame, radius=22, top_alpha=18, bottom_alpha=16)
    inner = (banner_frame[0] + 10, banner_frame[1] + 10, banner_frame[2] - 10, banner_frame[3] - 10)
    _draw_soft_glow(img, inner, radius=18, color=(0, 150, 60, 35), border=3)
    _rounded_rect(draw, inner, 18, fill=(238, 242, 239), outline=(35, 55, 58), width=1)

    left_panel = (inner[0], inner[1], inner[0] + 230, inner[3])
    right_panel = (inner[2] - 230, inner[1], inner[2], inner[3])
    draw.rectangle(left_panel, fill=(8, 20, 15))
    draw.rectangle(right_panel, fill=(8, 20, 15))

    for offset in range(0, 190, 22):
        draw.line((inner[0] + offset, inner[3], inner[0] + offset + 130, inner[1]), fill=(0, 90, 34), width=4)
        draw.line((inner[2] - offset, inner[3], inner[2] - offset - 130, inner[1]), fill=(0, 90, 34), width=4)

    center_x1 = inner[0] + 210
    center_x2 = inner[2] - 210
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle((center_x1 + 5, inner[1] + 8, center_x2 + 5, inner[3] + 8), radius=0, fill=(0, 0, 0, 36))
    shadow = shadow.filter(ImageFilter.GaussianBlur(10))
    img.alpha_composite(shadow)
    draw.rectangle((center_x1, inner[1], center_x2, inner[3]), fill=(239, 243, 240))

    title = "BALI BETS"
    if market_type == "live":
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

    for side in ("left", "right"):
        if side == "left":
            cx, cy = inner[0] + 108, inner[1] + 155
            handle = (cx + 34, cy + 58, cx + 72, cy + 126)
            ball = (inner[0] + 86, inner[3] - 74, inner[0] + 130, inner[3] - 30)
            paddle_fill = (124, 255, 0) if market_type == "live" else (202, 18, 25)
        else:
            cx, cy = inner[2] - 108, inner[1] + 155
            handle = (cx - 72, cy + 58, cx - 34, cy + 126)
            ball = (inner[2] - 130, inner[3] - 74, inner[2] - 86, inner[3] - 30)
            paddle_fill = (22, 24, 25)

        draw.ellipse((cx - 72, cy - 72, cx + 72, cy + 72), fill=paddle_fill, outline=(18, 18, 18), width=3)
        draw.rounded_rectangle(handle, radius=10, fill=(111, 62, 28), outline=(36, 24, 18), width=2)
        draw.ellipse(ball, fill=(235, 239, 236), outline=(175, 185, 180), width=2)

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

    plays = _collect_plays(row, forced_market_type=forced_market_type)
    market_type = forced_market_type or _detect_market_type(row, plays)
    for play in plays:
        play["market_type"] = market_type
    play_count = len(plays)
    primary_unit = _unit_display(plays[0].get("unit", "") or _get_value(row, "Unit", "Units", "Stake", fallback=""))
    single_moneyline_layout = market_type == "moneyline" and play_count == 1

    width = 1200

    # Strict layout grid. Every major module derives from these values.
    # Do not nudge individual pieces unless the grid constants change.
    OUTER_PAD = 26
    SHELL_INSET = 32
    INNER_PAD = 30
    ROW_GAP = 20
    LEFT_GUTTER = INNER_PAD
    RIGHT_GUTTER = INNER_PAD
    ICON_COL_W = 86
    TEXT_COL_GAP = 22
    BADGE_H = 56
    PLAY_ROW_H = 122
    BANNER_TOP_GAP = 38
    BOARD_TOP_GUTTER = INNER_PAD
    BOARD_BOTTOM_GUTTER = INNER_PAD

    green = (132, 255, 55)
    white = (246, 248, 250)
    off_white = (184, 193, 200)
    muted = (111, 123, 132)
    bg_top = (3, 6, 9)
    bg_bottom = (6, 10, 14)

    header_h = 84
    hero_h = 118
    chip_h = BADGE_H
    row_h = PLAY_ROW_H if single_moneyline_layout else 84
    row_gap = 0 if single_moneyline_layout else 12
    rows_h = play_count * row_h + max(0, play_count - 1) * row_gap
    banner_h = 358

    shell_x1 = OUTER_PAD
    shell_x2 = width - OUTER_PAD
    panel_x1 = shell_x1 + SHELL_INSET
    panel_x2 = shell_x2 - SHELL_INSET

    header_y = 34
    hero_y = header_y + header_h + ROW_GAP
    board_y = hero_y + hero_h + ROW_GAP
    top_y = board_y + BOARD_TOP_GUTTER
    rows_top = top_y + chip_h + (28 if single_moneyline_layout else 32)
    banner_y = rows_top + rows_h + BANNER_TOP_GAP
    board_bottom = banner_y + banner_h + BOARD_BOTTOM_GUTTER
    total_h = board_bottom + 40
    shell = (shell_x1, 18, shell_x2, total_h - 18)

    img = Image.new("RGBA", (width, total_h), bg_top + (255,))
    draw = ImageDraw.Draw(img)

    for y in range(total_h):
        t = y / max(1, total_h - 1)
        r = int(bg_top[0] * (1 - t) + bg_bottom[0] * t)
        g = int(bg_top[1] * (1 - t) + bg_bottom[1] * t)
        b = int(bg_top[2] * (1 - t) + bg_bottom[2] * t)
        draw.line((0, y, width, y), fill=(r, g, b, 255))

    for x in range(-220, width + 220, 190):
        draw.line((x, 0, x + 320, total_h), fill=(17, 24, 30, 16), width=1)
    for y in range(28, total_h, 64):
        draw.line((30, y, width - 30, y), fill=(10, 15, 20, 16), width=1)

    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((130, -120, width - 130, 320), fill=(255, 255, 255, 10))
    gd.ellipse((160, 220, width - 160, total_h - 120), fill=(48, 110, 70, 8))
    glow = glow.filter(ImageFilter.GaussianBlur(72))
    img.alpha_composite(glow)

    _draw_drop_shadow(img, shell, radius=30, offset=(0, 16), blur=28, alpha=92)
    _draw_glossy_panel(img, shell, 30, (10, 15, 19, 255), (5, 8, 11, 255), outline=(42, 54, 62), inner_outline=(255, 255, 255, 8), gloss_alpha=12)
    draw.rounded_rectangle((shell[0] + 12, shell[1] + 12, shell[2] - 12, shell[3] - 12), radius=26, outline=(14, 21, 27), width=1)

    # header
    header = (panel_x1, header_y, panel_x2, header_y + header_h)
    _draw_drop_shadow(img, header, radius=22, offset=(0, 8), blur=18, alpha=72)
    _draw_glossy_panel(img, header, 22, (16, 24, 30, 255), (7, 11, 15, 255), outline=(42, 56, 64), inner_outline=(255, 255, 255, 8), gloss_alpha=18)
    header_inner_x = header[0] + INNER_PAD
    header_logo_box = (header_inner_x, header[1] + 18, header_inner_x + 48, header[1] + 66)
    _paste_circle(img, AVATAR_PATH, header_logo_box, border=0)

    title_x = header_logo_box[2] + 20
    title_y = header[1] + 12
    alert_y = header[1] + 50
    draw.text((title_x, title_y), BRAND_NAME, font=_font(28, True), fill=white)
    alert_font = _font(16, True)
    alert_chip_text = "BET ALERT"
    alert_chip_w = _text_width(draw, alert_chip_text, alert_font) + 24
    alert_chip = (title_x, alert_y, title_x + alert_chip_w, alert_y + 26)
    _draw_glossy_panel(img, alert_chip, 12, (18, 25, 31, 255), (8, 12, 16, 255), outline=(44, 57, 66), inner_outline=(255, 255, 255, 8), gloss_alpha=14)
    _draw_text_vcenter(draw, alert_chip, alert_chip_text, alert_font, green, x=alert_chip[0] + 12)
    draw.text((alert_chip[2] + 16, alert_y + 3), "PLAY STARTING IN 5 MINUTES", font=alert_font, fill=muted)

    badge_w = 118
    badge = (header[2] - INNER_PAD - badge_w, header[1] + 14, header[2] - INNER_PAD, header[3] - 14)
    _draw_glossy_panel(img, badge, 18, (17, 25, 30, 255), (8, 12, 16, 255), outline=(44, 57, 66), inner_outline=(255, 255, 255, 8), gloss_alpha=16)
    _paste_contain(img, AVATAR_PATH, (badge[0] + 20, badge[1] + 8, badge[2] - 20, badge[3] - 8))

    # matchup
    hero = (panel_x1, hero_y, panel_x2, hero_y + hero_h)
    _draw_drop_shadow(img, hero, radius=24, offset=(0, 10), blur=20, alpha=78)
    _draw_glossy_panel(img, hero, 24, (13, 19, 23, 255), (6, 10, 13, 255), outline=(42, 56, 64), inner_outline=(255, 255, 255, 8), gloss_alpha=14)

    time_pill_w = 292
    time_pill = (hero[0] + LEFT_GUTTER, hero[1] + 22, hero[0] + LEFT_GUTTER + time_pill_w, hero[3] - 22)
    _draw_glossy_panel(img, time_pill, 18, (18, 25, 31, 255), (8, 12, 16, 255), outline=(44, 57, 66), inner_outline=(255, 255, 255, 10), gloss_alpha=20)
    clock_size = 46
    clock_y = int(time_pill[1] + ((time_pill[3] - time_pill[1]) - clock_size) / 2)
    _draw_clock(draw, time_pill[0] + 18, clock_y)
    clean_est = str(est).upper().replace("EST", "").replace("EDT", "").strip() or est
    time_text, time_font = _fit_text(draw, clean_est, 160, 30, True, 24)
    est_font = _font(16, True)
    time_group_h = _text_height(draw, time_text, time_font) + 5 + _text_height(draw, "EST", est_font)
    time_group_y = int(time_pill[1] + ((time_pill[3] - time_pill[1]) - time_group_h) / 2) - 1
    time_x = time_pill[0] + 92
    draw.text((time_x, time_group_y), time_text, font=time_font, fill=white)
    draw.text((time_x, time_group_y + _text_height(draw, time_text, time_font) + 5), "EST", font=est_font, fill=green)

    divider_x = time_pill[2] + 24
    draw.line((divider_x, time_pill[1] + 6, divider_x, time_pill[3] - 6), fill=(48, 60, 68), width=2)

    matchup = f"{player_1} vs {player_2}"
    tx = divider_x + 22
    matchup_text, matchup_font = _fit_text(draw, matchup, hero[2] - tx - RIGHT_GUTTER, 27, True, 18)
    matchup_label_font = _font(14, True)
    matchup_group_h = _text_height(draw, "MATCHUP", matchup_label_font) + 10 + _text_height(draw, matchup_text, matchup_font)
    matchup_group_y = int(hero[1] + ((hero[3] - hero[1]) - matchup_group_h) / 2) - 1
    draw.text((tx, matchup_group_y), "MATCHUP", font=matchup_label_font, fill=muted)
    ty = matchup_group_y + _text_height(draw, "MATCHUP", matchup_label_font) + 10
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
    board = (panel_x1, board_y, panel_x2, board_bottom)
    _draw_drop_shadow(img, board, radius=28, offset=(0, 14), blur=24, alpha=84)
    _draw_glossy_panel(img, board, 28, (18, 25, 30, 255), (8, 12, 16, 255), outline=(40, 53, 60), inner_outline=(255, 255, 255, 8), gloss_alpha=12)

    top_y = board_y + BOARD_TOP_GUTTER
    league_font = _font(25, True)
    league_text = str(league).upper()
    league_icon = _league_icon_path(league)
    league_text_w = _text_width(draw, league_text, league_font)
    league_chip_w = max(350, league_text_w + (106 if league_icon else 74))
    league_chip = (board[0] + LEFT_GUTTER, top_y, board[0] + LEFT_GUTTER + league_chip_w, top_y + chip_h)
    _draw_league_chip(img, draw, league_chip, league, text_color=white)

    if primary_unit:
        unit_text_w = _text_width(draw, primary_unit, _font(30, True))
        unit_chip_w = max(282, unit_text_w + 110)
        unit_chip = (board[2] - RIGHT_GUTTER - unit_chip_w, top_y, board[2] - RIGHT_GUTTER, top_y + chip_h)
        _draw_glossy_panel(img, unit_chip, 16, (18, 25, 31, 255), (8, 12, 16, 255), outline=(44, 57, 66), inner_outline=(255, 255, 255, 10), gloss_alpha=18)
        _draw_text_vcenter(draw, unit_chip, primary_unit, _font(30, True), green, x=unit_chip[0] + ((unit_chip[2] - unit_chip[0]) - unit_text_w) / 2)

    section_y = top_y + chip_h + 16
    if market_type == "moneyline":
        section = "" if play_count == 1 else "MONEYLINES"
    elif market_type == "live":
        section = "LIVE PLAYS"
    else:
        section = "OFFICIAL PLAYS"
    if section:
        draw.text((board[0] + LEFT_GUTTER, section_y), section, font=_font(18, True), fill=(205, 211, 216))
        if play_count > 1:
            draw.text((board[0] + LEFT_GUTTER + _text_width(draw, section, _font(18, True)) + 14, section_y + 4), f"• {play_count} PLAYS", font=_font(14, True), fill=muted)
        draw.line((board[0] + LEFT_GUTTER, section_y + 28, board[2] - RIGHT_GUTTER, section_y + 28), fill=(21, 30, 37), width=1)

    row_x1 = board[0] + LEFT_GUTTER
    row_x2 = board[2] - RIGHT_GUTTER
    current_y = rows_top
    for idx, play in enumerate(plays, start=1):
        row_box = (row_x1, current_y, row_x2, current_y + row_h)
        _draw_drop_shadow(img, row_box, radius=18, offset=(0, 8), blur=16, alpha=54)
        _draw_glossy_panel(img, row_box, 18, (16, 23, 28, 255), (8, 12, 16, 255), outline=(34, 45, 53), inner_outline=(255, 255, 255, 7), gloss_alpha=10)
        bet_text = str(play.get("bet", "") or "No Bet Found").strip()
        history_text = str(play.get("history", "") or "").strip()

        if single_moneyline_layout:
            # Fixed icon column + fixed text column. The icon, label, and pick
            # are one vertically centered group, not three separately nudged objects.
            check_size = 54
            icon_x = row_box[0] + 22
            icon_y = int(row_box[1] + ((row_box[3] - row_box[1]) - check_size) / 2)
            check_wrap = (icon_x, icon_y, icon_x + check_size, icon_y + check_size)
            _draw_glossy_panel(img, check_wrap, 15, (18, 25, 31, 255), (8, 12, 16, 255), outline=(44, 57, 66), inner_outline=(255, 255, 255, 8), gloss_alpha=10)
            green_check = (132, 255, 55)
            draw.line((check_wrap[0] + 15, check_wrap[1] + 29, check_wrap[0] + 24, check_wrap[1] + 38), fill=green_check, width=6)
            draw.line((check_wrap[0] + 23, check_wrap[1] + 38, check_wrap[0] + 38, check_wrap[1] + 18), fill=green_check, width=6)

            content_x = row_box[0] + ICON_COL_W + TEXT_COL_GAP
            content_w = row_box[2] - content_x - RIGHT_GUTTER
            sub_label = "OFFICIAL PLAY"
            label_font = _font(15, True)
            big_bet, big_font = _fit_text(draw, bet_text, content_w, 49, True, 39)
            text_gap = 6
            text_group_h = _text_height(draw, sub_label, label_font) + text_gap + _text_height(draw, big_bet, big_font)
            text_group_y = int(row_box[1] + ((row_box[3] - row_box[1]) - text_group_h) / 2) - 2
            draw.text((content_x, text_group_y), sub_label, font=label_font, fill=off_white)
            draw.text((content_x, text_group_y + _text_height(draw, sub_label, label_font) + text_gap), big_bet, font=big_font, fill=white)
        else:
            num_chip = (row_box[0] + 18, row_box[1] + 21, row_box[0] + 60, row_box[1] + 63)
            _draw_glossy_panel(img, num_chip, 14, (18, 25, 31, 255), (8, 12, 16, 255), outline=(44, 57, 66), inner_outline=(255, 255, 255, 8), gloss_alpha=16)
            n_txt = str(idx)
            n_w = _text_width(draw, n_txt, _font(20, True))
            _draw_text_vcenter(draw, num_chip, n_txt, _font(20, True), green, x=num_chip[0] + ((num_chip[2] - num_chip[0]) - n_w) / 2)

            check_x = num_chip[2] + 18
            _draw_check(draw, check_x, row_box[1] + 18)

            main_x = check_x + 56
            max_main_w = row_box[2] - main_x - 42

            if history_text and market_type != "moneyline":
                record = f"{history_text} L20"
                record_font = _font(20, True)
                bullet_font = _font(20, True)
                record_w = _text_width(draw, record, record_font)
                bullet_w = _text_width(draw, "  •  ", bullet_font)
                bet_fit, bet_font = _fit_text(draw, bet_text, max_main_w - record_w - bullet_w, 24, True, 16)
                draw.text((main_x, row_box[1] + 15), bet_fit, font=bet_font, fill=white)
                bet_w = _text_width(draw, bet_fit, bet_font)
                draw.text((main_x + bet_w, row_box[1] + 15), "  •  ", font=bullet_font, fill=off_white)
                draw.text((main_x + bet_w + bullet_w, row_box[1] + 15), record, font=record_font, fill=white)
            else:
                bet_fit, bet_font = _fit_text(draw, bet_text, max_main_w, 24, True, 16)
                draw.text((main_x, row_box[1] + 15), bet_fit, font=bet_font, fill=white)

            meta_parts = []
            scenario = str(play.get("scenario", "") or "").strip()
            if scenario:
                meta_parts.append(f"If {scenario}" if not scenario.lower().startswith("if ") else scenario)
            if play.get("unit"):
                meta_parts.append(_unit_display(play.get("unit", "")))
            if history_text and market_type == "totals":
                meta_parts.append("History " + history_text)
            if meta_parts:
                meta_line = "   •   ".join(meta_parts)
                meta_fit, meta_font = _fit_text(draw, meta_line, max_main_w, 14, False, 11)
                draw.text((main_x, row_box[1] + 46), meta_fit, font=meta_font, fill=off_white)

        current_y += row_h + row_gap

    banner_frame = (row_x1, banner_y, row_x2, banner_y + banner_h)
    _draw_drop_shadow(img, banner_frame, radius=22, offset=(0, 10), blur=18, alpha=66)
    _draw_glossy_panel(img, banner_frame, 22, (16, 23, 28, 255), (8, 12, 16, 255), outline=(36, 48, 56), inner_outline=(255, 255, 255, 8), gloss_alpha=12)
    banner_inner = (banner_frame[0] + 12, banner_frame[1] + 12, banner_frame[2] - 12, banner_frame[3] - 12)
    if market_type == "moneyline":
        if play_count == 1:
            banner_source = MONEYLINE_BANNER_PATH if MONEYLINE_BANNER_PATH.exists() else (MONEYLINES_BANNER_PATH if MONEYLINES_BANNER_PATH.exists() else BANNER_PATH)
        else:
            banner_source = MONEYLINES_BANNER_PATH if MONEYLINES_BANNER_PATH.exists() else (MONEYLINE_BANNER_PATH if MONEYLINE_BANNER_PATH.exists() else BANNER_PATH)
        _paste_cover(img, banner_source, banner_inner, radius=18)
    elif market_type == "live":
        _draw_market_banner(img, banner_frame, market_type)
    else:
        _paste_cover(img, BANNER_PATH, banner_inner, radius=18)

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
