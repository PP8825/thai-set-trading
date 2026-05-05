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

# ── Menu image dimensions (LINE requirement: width=2500, height≥843) ──────────
W, H = 2500, 843

# ── Colors ─────────────────────────────────────────────────────────────────────
BG_COLOR     = (15,  23,  42)   # dark navy
BTN_COLORS   = [
    (34, 139,  87),   # green  — Signal
    (37, 99,  235),   # blue   — Dividend
    (124, 58, 237),   # purple — Report
]
TEXT_COLOR   = (255, 255, 255)
BORDER_COLOR = (255, 255, 255)
DIVIDER      = (50,  65,  90)

BUTTONS = [
    ("📊", "Signal",   "signal"),
    ("💰", "Dividend", "dividend"),
    ("📋", "Report",   "report"),
]


def make_image():
    img  = Image.new("RGB", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    btn_w = W // 3

    # Try to load a font, fall back to default
    try:
        font_big   = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 120)
        font_small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc",  80)
        font_emoji = ImageFont.truetype("/System/Library/Fonts/Apple Color Emoji.ttc", 100)
    except Exception:
        font_big   = ImageFont.load_default()
        font_small = ImageFont.load_default()
        font_emoji = font_big

    for i, (emoji, label, _) in enumerate(BUTTONS):
        x0 = i * btn_w
        x1 = x0 + btn_w

        # Button background
        draw.rectangle([x0 + 10, 10, x1 - 10, H - 10],
                       fill=BTN_COLORS[i], outline=BORDER_COLOR, width=3)

        # Emoji
        ex = x0 + btn_w // 2
        draw.text((ex, H // 2 - 120), emoji, font=font_emoji,
                  fill=TEXT_COLOR, anchor="mm")

        # Label
        draw.text((ex, H // 2 + 60), label, font=font_big,
                  fill=TEXT_COLOR, anchor="mm")

        # Divider
        if i < 2:
            draw.line([(x1, 20), (x1, H - 20)], fill=DIVIDER, width=4)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


def create_rich_menu():
    menu = {
        "size":       {"width": W, "height": H},
        "selected":   True,
        "name":       "SET Trading Bot Menu",
        "chatBarText":"📊 Trading Menu",
        "areas": [
            {
                "bounds": {"x": i * (W // 3), "y": 0,
                           "width": W // 3, "height": H},
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
