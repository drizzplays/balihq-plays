import os
from PIL import Image, ImageDraw, ImageFont

# --- CORE SETTINGS (DO NOT TOUCH) ---
# Banner size, Fonts, Colors, Border radius, Outer card size, 
# Row heights, TT badge x-position, 2 UNITS box x-position, 
# and Header logo sizes are preserved exactly.

def create_bali_bet_card(data):
    # Base Canvas Setup
    card_width, card_height = 1000, 1200 # Standard card size
    img = Image.new('RGB', (card_width, card_height), color='#000000')
    draw = ImageDraw.Draw(img)
    
    # 1. HEADER SECTION
    # Move second line (BET ALERT / Time) down 1-2px
    header_title_y = 60
    header_subtitle_y = 110 + 1.5 # Adjusted
    
    # Move right logo box left 4-6px for better balance[cite: 1]
    right_logo_x = 880 - 5 # Adjusted[cite: 1]
    
    # 2. MATCHUP ROW
    # Move divider and text block left 6-8px (Clock box unchanged)[cite: 1]
    clock_box_x = 50 
    divider_x = 180 - 7 # Adjusted[cite: 1]
    matchup_text_x = 210 - 7 # Adjusted[cite: 1]
    
    # 3. LOWER CONTENT PANEL (The "Tight Stack" Fix)
    # Reduce matchup-to-lower-panel gap by 6-8px[cite: 1]
    lower_panel_top_y = 350 - 7 # Adjusted[cite: 1]
    
    # 4. TT / 2 UNITS ROW
    # Add 2-3px gap between TT icon and “TT ELITE”[cite: 1]
    tt_icon_width = 40
    tt_text_x = tt_icon_width + 15 + 2.5 # Adjusted[cite: 1]
    
    # Center “2 UNITS” text 1px up[cite: 1]
    two_units_box_y = lower_panel_top_y + 10
    two_units_text_y = two_units_box_y + 12 - 1 # Adjusted[cite: 1]
    
    # 5. OFFICIAL PLAY ROW
    # Reduce TT row-to-official row gap by 5-7px[cite: 1]
    official_row_y = lower_panel_top_y + 100 - 6 # Adjusted[cite: 1]
    
    # Move text block left 6-8px to close the "dead pocket"[cite: 1]
    # Move stack down 1-2px as a group[cite: 1]
    check_icon_x = 60
    official_text_x = 140 - 7 # Adjusted[cite: 1]
    official_text_y = official_row_y + 2 # Adjusted[cite: 1]
    
    # 6. BANNER SECTION
    # Move banner up 5-7px (Do not resize)[cite: 1]
    banner_y = 950 - 6 # Adjusted[cite: 1]
    
    # ... rest of drawing logic using updated coordinates ...
    return img

# Script execution for BaliHQ automation
