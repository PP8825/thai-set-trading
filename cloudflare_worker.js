// Cloudflare Worker — LINE webhook for SET Trading Bot
// Env vars (Worker Settings → Variables and Secrets):
//   LINE_CHANNEL_SECRET  — LINE Developer Console → Basic settings
//   LINE_TOKEN           — channel access token
//   GITHUB_PAT           — GitHub fine-grained PAT with Actions read+write

const REPO = "PP8825/thai-set-trading";

// Known command keywords
const COMMANDS = {
  signal:    { workflow: "signal-on-demand.yml",    reply: "🔍 Scanning signals… results in ~1 min" },
  signals:   { workflow: "signal-on-demand.yml",    reply: "🔍 Scanning signals… results in ~1 min" },
  scan:      { workflow: "signal-on-demand.yml",    reply: "🔍 Scanning signals… results in ~1 min" },
  สัญญาณ:    { workflow: "signal-on-demand.yml",    reply: "🔍 Scanning signals… results in ~1 min" },
  dividend:  { workflow: "dividend-on-demand.yml",  reply: "💰 Fetching top dividend stocks… ~1 min" },
  dividends: { workflow: "dividend-on-demand.yml",  reply: "💰 Fetching top dividend stocks… ~1 min" },
  ปันผล:     { workflow: "dividend-on-demand.yml",  reply: "💰 Fetching top dividend stocks… ~1 min" },
  report:    { workflow: "report-on-demand.yml",    reply: "📋 Generating portfolio report… ~1 min" },
  รายงาน:    { workflow: "report-on-demand.yml",    reply: "📋 Generating portfolio report… ~1 min" },
  watchlist: { workflow: "watchlist-on-demand.yml", reply: "📋 Loading your watchlist… ~1 min" },
  วอชลิสต์:  { workflow: "watchlist-on-demand.yml", reply: "📋 Loading your watchlist… ~1 min" },
};

// Looks like a stock name: 2-10 chars, letters/digits only, no spaces
function looksLikeStock(text) {
  return /^[a-zA-Z0-9]{2,10}$/.test(text);
}

// Looks like add/remove command: "add PTT" or "remove AMATA"
function parseWatchlistCmd(text) {
  const m = text.match(/^(add|remove)\s+([a-zA-Z0-9]{2,10})$/i);
  if (m) return { action: m[1].toLowerCase(), stock: m[2].toUpperCase() };
  return null;
}

async function triggerGitHub(pat, workflow, inputs = {}) {
  const res = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${pat}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "SET-Trading-Bot",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ ref: "main", inputs }),
    }
  );
  return res.status; // 204 = success
}

async function lineReply(replyToken, text, lineToken) {
  if (!replyToken || !lineToken) return;
  await fetch("https://api.line.me/v2/bot/message/reply", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${lineToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      replyToken,
      messages: [{ type: "text", text }],
    }),
  });
}

export default {
  async fetch(request, env) {
    if (request.method === "GET") {
      return new Response("SET Trading Bot webhook is alive", { status: 200 });
    }
    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405 });
    }

    const rawBody = await request.text();

    // Verify LINE signature
    if (env.LINE_CHANNEL_SECRET) {
      const sig  = request.headers.get("x-line-signature") || "";
      const key  = await crypto.subtle.importKey(
        "raw", new TextEncoder().encode(env.LINE_CHANNEL_SECRET),
        { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
      );
      const mac  = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(rawBody));
      const expected = btoa(String.fromCharCode(...new Uint8Array(mac)));
      if (sig !== expected) return new Response("Invalid signature", { status: 401 });
    }

    let payload;
    try { payload = JSON.parse(rawBody); }
    catch { return new Response("Bad JSON", { status: 400 }); }

    for (const event of payload.events || []) {
      if (event.type !== "message") continue;
      const raw  = (event.message?.text || "").trim();
      const text = raw.toLowerCase();

      let reply, workflow, inputs = {};

      const wlCmd = parseWatchlistCmd(text);

      if (COMMANDS[text]) {
        // Known command
        workflow = COMMANDS[text].workflow;
        reply    = COMMANDS[text].reply;
      } else if (wlCmd) {
        // add / remove watchlist command
        workflow = "watchlist-manage.yml";
        inputs   = { action: wlCmd.action, stock: wlCmd.stock };
        reply    = wlCmd.action === "add"
          ? `➕ Adding ${wlCmd.stock} to watchlist…`
          : `➖ Removing ${wlCmd.stock} from watchlist…`;
      } else if (looksLikeStock(raw)) {
        // Stock name lookup
        workflow = "stock-lookup.yml";
        inputs   = { stock: raw.toUpperCase() };
        reply    = `🔍 Looking up ${raw.toUpperCase()}… results in ~1 min`;
      } else {
        continue; // ignore everything else
      }

      try {
        const status = await triggerGitHub(env.GITHUB_PAT, workflow, inputs);
        if (status !== 204) reply = `⚠️ GitHub returned ${status}`;
      } catch (e) {
        reply = `❌ Error: ${String(e).slice(0, 80)}`;
      }

      await lineReply(event.replyToken, reply, env.LINE_TOKEN);
    }

    return new Response("OK", { status: 200 });
  },
};
