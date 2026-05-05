// Cloudflare Worker — LINE webhook for SET Trading Bot
// Env vars (Worker Settings → Variables and Secrets):
//   LINE_CHANNEL_SECRET  — LINE Developer Console → Basic settings
//   LINE_TOKEN           — channel access token
//   GITHUB_PAT           — GitHub fine-grained PAT with Actions read+write

const REPO = "PP8825/thai-set-trading";

const ACTIONS = {
  signal:   "signal-on-demand.yml",
  signals:  "signal-on-demand.yml",
  scan:     "signal-on-demand.yml",
  สัญญาณ:   "signal-on-demand.yml",
  dividend: "dividend-on-demand.yml",
  dividends:"dividend-on-demand.yml",
  ปันผล:    "dividend-on-demand.yml",
  report:   "report-on-demand.yml",
  รายงาน:   "report-on-demand.yml",
};

async function triggerGitHub(pat, workflow) {
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
      body: JSON.stringify({ ref: "main" }),
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

const REPLIES = {
  "signal-on-demand.yml":   "🔍 Scanning signals… results in ~1 min",
  "dividend-on-demand.yml": "💰 Fetching top dividend stocks… results in ~1 min",
  "report-on-demand.yml":   "📋 Generating portfolio report… results in ~1 min",
};

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
      const text     = (event.message?.text || "").trim().toLowerCase();
      const workflow = ACTIONS[text];
      if (!workflow) continue;

      let reply;
      try {
        const status = await triggerGitHub(env.GITHUB_PAT, workflow);
        reply = status === 204
          ? REPLIES[workflow]
          : `⚠️ GitHub returned ${status}`;
      } catch (e) {
        reply = `❌ Error: ${String(e).slice(0, 80)}`;
      }
      await lineReply(event.replyToken, reply, env.LINE_TOKEN);
    }

    return new Response("OK", { status: 200 });
  },
};
