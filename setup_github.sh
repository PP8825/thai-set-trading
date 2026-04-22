#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  Thai SET Trading System — GitHub Actions Setup
#  Run once from Terminal: bash setup_github.sh
# ─────────────────────────────────────────────────────────────

set -e
STOCK_DIR="$HOME/Documents/Claude/Projects/Stock"
REPO_NAME="thai-set-trading"

echo "=================================================="
echo "  Thai SET Trading — GitHub Actions Setup"
echo "=================================================="
echo ""

# 1. Install GitHub CLI if needed
if ! command -v gh &>/dev/null; then
    echo "Installing GitHub CLI (gh)..."
    if command -v brew &>/dev/null; then
        brew install gh
    else
        echo "ERROR: Homebrew not found. Install from https://brew.sh first, then re-run."
        exit 1
    fi
fi

# 2. Login to GitHub
echo "Step 1: Log in to GitHub"
echo "(A browser window will open — sign in and authorise)"
echo ""
gh auth login --web --git-protocol https

# 3. Read LINE credentials from config
LINE_TOKEN=$(python3 -c "import json; d=json.load(open('$STOCK_DIR/set_config.json')); print(d.get('line_channel_access_token',''))" 2>/dev/null || echo "")
LINE_USER=$(python3 -c "import json; d=json.load(open('$STOCK_DIR/set_config.json')); print(d.get('line_user_id',''))" 2>/dev/null || echo "")

if [ -z "$LINE_TOKEN" ] || [ -z "$LINE_USER" ]; then
    echo ""
    echo "LINE credentials not found in set_config.json."
    read -p "Enter your LINE Channel Access Token: " LINE_TOKEN
    read -p "Enter your LINE User ID (starts with U): " LINE_USER
fi

# 4. Create GitHub repo
echo ""
echo "Step 2: Creating private GitHub repository '$REPO_NAME'..."
cd "$STOCK_DIR"

# Init git
git init -q
git checkout -b main 2>/dev/null || true

# Stage all files (excluding sensitive/temp files)
git add \
    set_realtime_monitor.py \
    set_eod_report.py \
    set_backtest.py \
    set_test_run.py \
    set_config.json \
    set_portfolio.json \
    set_signal_state.json \
    .github/ \
    .gitignore \
    Portfolio_Reports/.gitkeep 2>/dev/null || true

git commit -q -m "Initial commit: Thai SET trading system" 2>/dev/null || \
git commit -q --allow-empty -m "Initial commit" 2>/dev/null || true

# Create repo and push
gh repo create "$REPO_NAME" \
    --private \
    --description "Thai SET stock trading system with LINE alerts" \
    --source=. \
    --remote=origin \
    --push 2>/dev/null || {
    echo "Repo may already exist — pushing to existing repo..."
    git push -u origin main --force 2>/dev/null || true
}

# 5. Set GitHub Secrets
echo ""
echo "Step 3: Setting LINE secrets..."
gh secret set LINE_TOKEN     --body "$LINE_TOKEN"
gh secret set LINE_USER_ID   --body "$LINE_USER"

# 6. Done
GITHUB_USER=$(gh api user -q .login)
echo ""
echo "=================================================="
echo "  ✅  Setup complete!"
echo "=================================================="
echo ""
echo "Your repo: https://github.com/$GITHUB_USER/$REPO_NAME"
echo ""
echo "GitHub Actions will now run automatically:"
echo "  • Every 15 min during Bangkok market hours"
echo "    (Mon-Fri 10:00-12:30, 14:30-16:30)"
echo "  • EOD Excel report at 4:35 PM Bangkok"
echo ""
echo "To trigger a test run right now:"
echo "  gh workflow run realtime-monitor.yml --repo $GITHUB_USER/$REPO_NAME"
echo ""
echo "To view run logs:"
echo "  https://github.com/$GITHUB_USER/$REPO_NAME/actions"
echo ""
