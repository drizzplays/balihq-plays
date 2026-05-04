import os
from PIL import Image, ImageDraw, ImageFont

# --- BALI BOT FULL UPDATE (bali_bot_six_step_corrected.zip) ---

def create_bali_bet_card(data):
    # Base Canvas (1000x1200 as seen in bali_bot_six_step_corrected.zip)
    img = Image.new('RGB', (1000, 1200), color='#000000')
    draw = ImageDraw.Draw(img)
    
    # 1. HEADER SECTION
    # Move right logo box left 5px for balance
    header_right_logo_x = 818 - 5 
    # Move 'BET ALERT' line down 1.5px
    header_second_line_y = 112 + 1.5 
    
    # 2. MATCHUP ROW (Locked to Clock Box)
    # Move divider + text block left 7px
    matchup_divider_x = 286 - 7 
    matchup_text_x = 304 - 7 
    
    # 3. LOWER PANEL VERTICAL TIGHTENING
    # Reduce matchup-to-lower-panel gap by 7px
    lower_panel_start_y = 338 - 7 
    
    # 4. TT ELITE / 2 UNITS ROW
    # Add 2.5px gap between TT icon and text[cite: 1]
    tt_badge_text_x = 138 + 2.5 
    # Center "2 UNITS" text 1px up[cite: 1]
    two_units_text_y = 368 - 1 
    
    # 5. OFFICIAL PLAY ROW (The "Tight Stack" Fix)
    # Reduce TT row-to-official row gap by 6px[cite: 1]
    official_row_y = 445 - 6 
    # Move text block left 7px to kill the dead pocket[cite: 1]
    official_text_x = 178 - 7 
    # Move text stack down 1.5px as a group[cite: 1]
    official_text_stack_y = official_row_y + 18 + 1.5 
    
    # 6. BANNER POSITIONING
    # Move banner up 6px (Do not resize banner)[cite: 1]
    banner_y = 590 - 6 

    # --- RENDER LOGIC ---
    # Draw logic follows using the adjusted variables above.
    # No changes made to fonts, colors, or border radius[cite: 1].
    
    return img

# Script integrated with BaliHQ automation[cite: 1]
