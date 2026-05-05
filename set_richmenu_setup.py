#!/usr/bin/env python3
"""
set_richmenu_setup.py
────────────────────────────────────────────────────────────────────
Creates and registers a LINE Rich Menu with 3 buttons:
  📊 Signal  |  💰 Dividend  |  📋 Report

Run this ONCE from your Mac:
  python set_richmenu_setup.py

Requirements:
  pip install Pillow requests
"""

import sys, os, json, io

def ensure_packages():
    import importlib, subprocess
    for pkg in ["Pillow", "requests"]:
        mod = "PIL" if pkg == "Pillow" else pkg
        try:
            importlib.import_module(mod)
        except ImportError:
            print(f"Installing {pkg}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

ensure_packages()

from PIL import Image, ImageDraw, ImageFont
import requests

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "set_config.json")

with open(CONFIG_PATH) as f:
    cfg = json.load(f)

LINE_TOKEN   = os.environ.get("LINE_TOKEN",   cfg.get("line_channel_access_token", ""))
LINE_USER_ID = os.environ.get("LINE_USER_ID", cfg.get("line_user_id", ""))

# ── Menu image dimensions — full height for 2×2 grid ─────────────────────────
W, H = 2500, 1686

# ── Colors ─────────────────────────────────────────────────────────────────────
BG_COLOR     = (13,  17,  27)   # deep dark navy
TEXT_COLOR   = (240, 245, 255)  # near-white
BORDER_COLOR = (255, 255, 255)

BUTTONS = [
    ("📊", "Signal",   "signal"),
    ("💰", "Dividend", "dividend"),
    ("📋", "Report",   "report"),
    ("⭐", "Watchlist","watchlist"),
]


def load_font(size):
    paths = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Bold.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/SFNSDisplay.ttf",
        "/System/Library/Fonts/Geneva.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def make_image():
    # 2×2 grid — pastel panels
    PANELS  = [
        (209, 236, 220),  # mint        — Signal
        (207, 226, 255),  # sky blue    — Dividend
        (255, 230, 210),  # peach       — Report
        (226, 215, 245),  # lavender    — Watchlist
    ]
    ACCENTS = [
        (39,  110,  70),  # deep green
        (30,   80, 180),  # deep blue
        (180,  80,  20),  # deep orange
        (90,   50, 160),  # deep purple
    ]
    SUBTITLES = [
        "Buy · Sell signals",
        "Top dividend yield",
        "Portfolio snapshot",
        "My stock watchlist",
    ]
    BG = (235, 237, 243)

    img       = Image.new("RGB", (W, H), BG)
    draw      = ImageDraw.Draw(img)
    font_main = load_font(140)
    font_sub  = load_font(62)
    GAP       = 10
    COLS, ROWS = 2, 2
    btn_w     = W // COLS
    btn_h     = H // ROWS

    for i, (_, label, _) in enumerate(BUTTONS):
        col = i % COLS
        row = i // COLS
        x0  = col * btn_w + GAP
        x1  = (col + 1) * btn_w - GAP
        y0  = row * btn_h + GAP
        y1  = (row + 1) * btn_h - GAP
        cx  = (x0 + x1) // 2
        cy  = (y0 + y1) // 2

        # Pastel panel
        draw.rectangle([x0, y0, x1, y1], fill=PANELS[i])

        # Accent top stripe
        draw.rectangle([x0, y0, x1, y0 + 12], fill=ACCENTS[i])

        # Main label
        draw.text((cx, cy - 45), label.upper(),
                  font=font_main, fill=ACCENTS[i], anchor="mm")

        # Subtitle
        draw.text((cx, cy + 80), SUBTITLES[i],
                  font=font_sub, fill=(100, 100, 120), anchor="mm")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


def create_rich_menu():
    COLS, ROWS = 2, 2
    btn_w = W // COLS
    btn_h = H // ROWS

    menu = {
        "size":        {"width": W, "height": H},
        "selected":    True,
        "name":        "SET Trading Bot Menu",
        "chatBarText": "📊 Trading Menu",
        "areas": [
            {
                "bounds": {
                    "x":      (i % COLS) * btn_w,
                    "y":      (i // COLS) * btn_h,
                    "width":  btn_w,
                    "height": btn_h,
                },
                "action": {"type": "message", "text": kw}
            }
            for i, (_, _, kw) in enumerate(BUTTONS)
        ]
    }

    # 1. Create rich menu
    r = requests.post(
        "https://api.line.me/v2/bot/richmenu",
        headers={"Authorization": f"Bearer {LINE_TOKEN}",
                 "Content-Type": "application/json"},
        json=menu, timeout=15
    )
    r.raise_for_status()
    menu_id = r.json()["richMenuId"]
    print(f"✅ Rich menu created: {menu_id}")

    # 2. Upload image
    image_bytes = make_image()
    r = requests.post(
        f"https://api-data.line.me/v2/bot/richmenu/{menu_id}/content",
        headers={"Authorization": f"Bearer {LINE_TOKEN}",
                 "Content-Type": "image/png"},
        data=image_bytes, timeout=30
    )
    r.raise_for_status()
    print("✅ Menu image uploaded")

    # 3. Set as default menu for all users
    r = requests.post(
        f"https://api.line.me/v2/bot/user/all/richmenu/{menu_id}",
        headers={"Authorization": f"Bearer {LINE_TOKEN}"},
        timeout=15
    )
    r.raise_for_status()
    print("✅ Rich menu set as default for all users")
    print(f"\n🎉 Done! Rich menu is live. Open LINE to see the menu at the bottom of the chat.")
    return menu_id


def delete_all_rich_menus():
    """Remove all existing rich menus before creating a new one."""
    r = requests.get(
        "https://api.line.me/v2/bot/richmenu/list",
        headers={"Authorization": f"Bearer {LINE_TOKEN}"}, timeout=10
    )
    menus = r.json().get("richmenus", [])
    for m in menus:
        mid = m["richMenuId"]
        requests.delete(
            f"https://api.line.me/v2/bot/richmenu/{mid}",
            headers={"Authorization": f"Bearer {LINE_TOKEN}"}, timeout=10
        )
        print(f"  Deleted old menu: {mid}")


if __name__ == "__main__":
    if not LINE_TOKEN:
        print("❌ LINE_TOKEN not found. Check set_config.json or set LINE_TOKEN env var.")
        sys.exit(1)

    print("Removing old rich menus...")
    delete_all_rich_menus()

    print("Creating new rich menu...")
    create_rich_menu()
