/* FATHER SIGNAL BOT - Telegram Mini App (static, no server).
   Data arrives in the URL (?d=base64url(JSON)); actions go back with Telegram.WebApp.sendData. */
(function () {
  "use strict";
  var PATTERNS = ["Bullish Engulfing", "Bearish Engulfing", "Hammer", "Shooting Star", "Morning Star",
    "Evening Star", "Bullish Marubozu", "Bearish Marubozu", "Tweezer Bottom", "Tweezer Top"]; // same order as father/patterns.py
  var SETTINGS = { // must match father/config.py TUNABLES (the bot validates again)
    min_score: [50, 100, "Minimum engineering score to alert"],
    max_spread_bps: [0.5, 50, "Max bid/ask spread (bps)"],
    min_volume: [1e6, 1e11, "Min 24h quote volume (USDT)"],
    volume_spike: [1, 10, "Volume spike ratio vs 20-candle avg"],
    cooldown_min: [0, 1440, "Minutes before re-alerting same coin+side"],
    universe_size: [5, 200, "Number of coins to scan"],
    require_volume: [0, 1, "1 = alert only with a volume spike"],
    max_alerts: [1, 50, "Max alerts per scan"],
    max_workers: [1, 8, "Parallel downloads"],
    tp_atr: [0.2, 10, "Backtest take-profit (ATR)"],
    sl_atr: [0.2, 10, "Backtest stop-loss (ATR)"],
    max_hold: [2, 500, "Backtest time stop (bars)"]
  };
  var tg = window.Telegram && window.Telegram.WebApp;
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); };

  function params() {
    var q = new URLSearchParams(location.search);
    var h = new URLSearchParams((location.hash || "").replace(/^#/, ""));
    return { d: q.get("d") || h.get("d"), k: q.get("k") || h.get("k") };
  }
  function decode(s) {
    if (!s) return null;
    try {
      var b = s.replace(/-/g, "+").replace(/_/g, "/");
      while (b.length % 4) b += "=";
      var bin = atob(b), bytes = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      return JSON.parse(new TextDecoder().decode(bytes));
    } catch (e) { return null; }
  }
  function pkt(ts) {
    if (!ts) return "-";
    try {
      return new Date(ts * 1000).toLocaleString("en-GB", { timeZone: "Asia/Karachi", day: "2-digit",
        month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
    } catch (e) { return new Date(ts * 1000).toISOString().slice(5, 16).replace("T", " ") + " UTC"; }
  }
  function ago(ts) {
    if (!ts) return "";
    var m = Math.round((Date.now() / 1000 - ts) / 60);
    return m < 1 ? "just now" : m < 60 ? m + " min ago" : Math.round(m / 60) + " h ago";
  }
  function fmt(x) { return x == null ? "-" : (Math.abs(x) >= 1000 ? x.toLocaleString("en-US", { maximumFractionDigits: 2 }) : String(x)); }

  var P = params(), D = decode(P.d);
  // sendData only works when opened from the bot's keyboard button (k=1) - not from the menu button/inline/link.
  var canSend = !!(tg && typeof tg.sendData === "function" && P.k === "1" && tg.platform !== "unknown" &&
    !(tg.initDataUnsafe && tg.initDataUnsafe.query_id));

  if (tg) {
    tg.ready(); tg.expand();
    try { tg.setHeaderColor("bg_color"); tg.setBackgroundColor("bg_color"); } catch (e) {}
  }

  function banner(msg) { var b = $("banner"); b.innerHTML += (b.innerHTML ? "<br>" : "") + msg; b.classList.remove("hidden"); }
  if (!D) banner("No data in this link. Send <b>/app</b> in the bot chat to get a fresh 📊 Father App button.");
  if (!canSend) banner((D ? "Read-only view (data from " + esc(pkt(D.t)) + " PKT). " : "") +
    "Buttons work only when opened from the <b>📊 Father App</b> keyboard button in chat — send <b>/app</b> to get it.");

  function send(obj) {
    if (!canSend) { if (tg && tg.showAlert) tg.showAlert("Open the app from the 📊 Father App keyboard button (send /app) to use buttons."); return; }
    var s = JSON.stringify(obj);
    if (s.length > 4000) return;
    try { tg.HapticFeedback && tg.HapticFeedback.impactOccurred("medium"); } catch (e) {}
    tg.sendData(s); // closes the app; the bot replies in chat
  }

  // ---------- tabs
  Array.prototype.forEach.call(document.querySelectorAll(".tabs button"), function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll(".tabs button").forEach(function (x) { x.classList.remove("active"); });
      document.querySelectorAll(".tab").forEach(function (x) { x.classList.remove("active"); });
      b.classList.add("active"); $(b.dataset.tab).classList.add("active");
      try { tg && tg.HapticFeedback && tg.HapticFeedback.selectionChanged(); } catch (e) {}
    });
  });

  // ---------- render
  var s = (D && D.s) || {};
  $("age").textContent = D ? "Data: " + pkt(D.t) + " PKT · " + ago(D.t) : "No data";
  var pill = $("scanPill");
  if (D) { pill.textContent = s.on ? "SCANNER ON" : "SCANNER OFF"; pill.className = "pill " + (s.on ? "on" : "off"); }
  $("stScan").textContent = D ? (s.on ? "ON" : "OFF") + (s.bt ? " · BT running" : "") : "-";
  $("stCoins").textContent = D ? s.n : "-";
  $("stLast").textContent = D && s.ls ? pkt(s.ls) : "-";
  $("stErr").textContent = D ? s.e : "-";

  function sigHtml(g) {
    var side = g[1] === "L" ? "LONG" : "SHORT", pat = PATTERNS[g[2]] || "?";
    return '<div class="sig"><span class="side ' + esc(g[1]) + '">' + side + '</span>' +
      '<div><div class="sym">' + esc(g[0]) + '</div><div class="meta">' + esc(pat) + " · " + esc(pkt(g[7])) + " PKT</div></div>" +
      '<div class="score">' + esc(g[3]) + "<small>score</small></div>" +
      '<div class="lv">Entry ' + esc(fmt(g[4])) + " · SL " + esc(fmt(g[5])) + " · TP " + esc(fmt(g[6])) + "</div></div>";
  }
  var sigs = (D && D.g) || [];
  $("sigList").innerHTML = sigs.length ? sigs.map(sigHtml).join("") : '<div class="empty">No signals yet.</div>';
  $("dashSig").innerHTML = sigs.length ? sigs.slice(0, 3).map(sigHtml).join("") : '<div class="empty">No signals yet.</div>';

  var b = (D && D.b) || {};
  $("btMeta").textContent = b.id ? "· " + b.id + " · " + pkt(b.ts) + " PKT" : "";
  if (b.o && b.o.length) {
    $("btOverall").innerHTML = (b.pm ? '<p class="hint">' + esc(b.pm) + "</p>" : "") +
      "<table><tr><th>TF</th><th>Group</th><th>N</th><th>Hist WR%</th><th>PF</th><th>Net%</th></tr>" +
      b.o.map(function (r) { return "<tr><td>" + esc(r[0]) + "</td><td>" + esc(r[1]) + "</td><td>" + esc(r[2]) +
        "</td><td>" + esc(r[3]) + "</td><td>" + esc(r[4] >= 999 ? "∞" : r[4]) + "</td><td>" + esc(r[5]) + "</td></tr>"; }).join("") +
      "</table><p class=\"hint\">Unseen test split, after fees + slippage. Past results, not a prediction.</p>";
  } else $("btOverall").innerHTML = '<div class="empty">No backtest yet. Run one above.</div>';
  if (b.p && b.p.length) {
    $("btPat").innerHTML = "<table><tr><th>TF</th><th>Pattern</th><th>N</th><th>PF</th><th>Avg%</th><th></th></tr>" +
      b.p.map(function (r) { return "<tr><td>" + esc(r[0]) + "</td><td>" + esc(PATTERNS[r[1]] || "?") + "</td><td>" + esc(r[2]) +
        "</td><td>" + esc(r[3] >= 999 ? "∞" : r[3]) + "</td><td>" + esc(r[4]) + '</td><td><span class="v ' + esc(r[5]) + '">' + esc(r[5]) + "</span></td></tr>"; }).join("") + "</table>";
  } else $("btPat").innerHTML = '<div class="empty">No pattern results yet.</div>';

  var c = (D && D.c) || {};
  $("setList").innerHTML = Object.keys(c).length ? Object.keys(c).map(function (k) {
    return '<div class="kv"><span>' + esc(k) + "</span><b>" + esc(fmt(c[k])) + "</b></div>"; }).join("") :
    '<div class="empty">No data.</div>';
  var sel = $("setKey");
  Object.keys(SETTINGS).forEach(function (k) { var o = document.createElement("option"); o.value = k; o.textContent = k; sel.appendChild(o); });
  function setHint() { var r = SETTINGS[sel.value]; $("setHint").textContent = r[2] + " (" + r[0] + " – " + r[1] + ")";
    $("setVal").value = c[sel.value] != null ? c[sel.value] : ""; }
  sel.addEventListener("change", setHint); setHint();

  // ---------- actions
  document.querySelectorAll("button.act[data-a]").forEach(function (btn) {
    btn.addEventListener("click", function () { send({ a: btn.dataset.a }); });
  });
  $("btRun").addEventListener("click", function () {
    var n = parseInt($("btSym").value, 10), d = parseFloat($("btDays").value);
    if (!(n >= 1 && n <= 100) || !(d >= 0.5 && d <= 30)) { (tg && tg.showAlert ? tg.showAlert : alert)("Coins 1–100, days 0.5–30"); return; }
    send({ a: "backtest", symbols: n, days: d });
  });
  $("setBtn").addEventListener("click", function () {
    var k = sel.value, v = parseFloat($("setVal").value), r = SETTINGS[k];
    if (!r || isNaN(v) || v < r[0] || v > r[1]) { (tg && tg.showAlert ? tg.showAlert : alert)(k + " must be " + r[0] + " – " + r[1]); return; }
    send({ a: "set", key: k, value: v });
  });
  var startTab = new URLSearchParams(location.search).get("tab");
  if (startTab && $(startTab)) { var tb = document.querySelector('.tabs button[data-tab="' + startTab + '"]'); if (tb) tb.click(); }
  if (!canSend) document.querySelectorAll("button.act").forEach(function (x) { x.disabled = true; });
})();
