# FATHER SIGNAL BOT

Analysis-only scalping signal bot for **Binance USDⓈ-M Futures**, made to run on an **Android phone with Termux**.
You control it from **Telegram**.

> **Analysis only.** It does not trade, does not place orders, does not connect a wallet and does not need a Binance API key.
> It only reads public market data. It makes **no win-rate claims**. The "Signal Score" is an **engineering score**
> (how many quality checks passed), **not** a probability and **not** a win rate.

---

## Quick install on Termux (one command)

Install **Termux from F-Droid** (or GitHub). The Play Store version is old. Then paste this:

```bash
pkg install -y git python && git clone https://github.com/ahmadrahmani147370-hype/father-signal-bot && cd father-signal-bot && bash setup_termux.sh
```

Then:

```bash
nano .env        # put TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, save with Ctrl+O, Enter, Ctrl+X
./start.sh       # starts the bot (wake-lock + auto-restart)
```

Open your bot in Telegram and send `/help`.

### Make the Telegram bot (free)
1. In Telegram open **@BotFather**, send `/newbot`, follow the steps, copy the **token**.
2. Open your new bot and press **Start** (send any message).
3. Get your **chat id**: open **@userinfobot** and copy your id
   (or open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and read `"chat":{"id":...}`).
4. Put both in `.env`. The bot only accepts commands from this chat id.

### pandas / numpy on Termux
`pip install pandas` usually fails on phones (it tries to compile). The setup script uses prebuilt packages instead:
```bash
pkg install python-numpy tur-repo
pkg install python-pandas
pip install requests python-dotenv
```
On a normal PC: `pip install -r requirements.txt`.

---

## 📊 Father App (Telegram Mini App) - no server needed

The bot has a full dashboard inside Telegram: **Dashboard, Signals, Backtest, Settings**.

- The app is a static page hosted free on **GitHub Pages**:
  https://ahmadrahmani147370-hype.github.io/father-signal-bot/webapp/
- Send **/app** (or /start, /help). The bot shows a **📊 Father App** keyboard button. Tap it.
- The bot puts its current data into the button link (status, last ~15 signals with entry/SL/TP reference levels,
  latest backtest summary, settings). Data is a snapshot from the moment the button was sent; the app shows its age.
- Buttons in the app (Scan On / Off / Once, Run Backtest, Save setting, Refresh) send the action back to the bot with
  `Telegram.WebApp.sendData`. The app closes, the bot does the action, replies in chat and sends a **fresh** 📊 button.
- Only your `TELEGRAM_CHAT_ID` is accepted. Actions and setting keys are checked against a whitelist.
- The **menu button** (bottom-left in the chat) also opens the app, but Telegram does not allow menu launches to send
  data back, so that view is **read-only**. Use the 📊 keyboard button for actions.
- Your phone does not need to host anything. If you fork the repo, change `WEBAPP_URL` in `.env` to your own Pages URL.


| Command | What it does |
|---|---|
| `/start`, `/help` | Show commands |
| `/status` | Scanner on/off, coins, last scan time, errors in last hour, API weight |
| `/scan_on` / `/scan_off` | Start / pause automatic scanning (one check per closed 1m candle) |
| `/scan_once` | Run one scan now |
| `/backtest [symbols] [days]` | e.g. `/backtest 5 3` = top 5 coins, last 3 days. Runs in the background, sends a summary, a per-pattern table and a CSV file |
| `/report` | Latest backtest results per pattern |
| `/signals [n]` | Last n signals |
| `/performance` | Tracked results of past alerts: did each one reach its TP or SL reference level first (or the time stop)? After fees + slippage |
| `/coins` | Current coin universe (volume, spread) |
| `/settings` | Show settings you can change |
| `/set key value` | e.g. `/set min_score 80`, `/set max_spread_bps 3`, `/set min_volume 50000000` |
| `/errors` | Recent scanner errors |
| `/app` | Open the 📊 Father App (Mini App) |

Settings changed with `/set` are saved in the database and survive restarts.

Debug only (no Telegram needed, alerts print to the screen):
```bash
python main.py --debug once
python main.py --debug backtest 5 3
python main.py --debug report
python main.py --debug test-telegram
python -m unittest discover -s tests -v
```

---

## Signal format

```
FATHER SIGNAL BOT

LONG SIGNAL

Coin: BTCUSDT
Exchange: Binance Futures
Entry Timeframe: 1M
Confirmation: 5M
Pattern: Bullish Engulfing
Volume: Confirmed
Trend: Bullish
RSI: 56.4
Signal Score: 85/100

Reasons:

- Bullish pattern detected.
- 5m trend aligned.
- Volume confirmation.
- Momentum confirmation.

Status: Analysis Only.
```

---

## How it works

**Market scanner** (`father/scanner.py`)
- Universe = USDT perpetual contracts with status `TRADING`, sorted by 24h quote volume, top 100.
- Filters: minimum 24h volume (`MIN_QUOTE_VOLUME_USDT`), maximum bid/ask spread (`MAX_SPREAD_BPS`, default 8 bps; min volume default 10M USDT - about 95-100 coins pass),
  minimum trade count, valid candle history (enough candles, no gaps, not stale).
  With strict filters you may get fewer than 100 coins - that is on purpose.
- Universe refresh every 15 minutes.
- Polls once per minute, a few seconds after each 1m candle closes. The live (unclosed) candle is always dropped.
- Candles are cached in SQLite; only new candles are downloaded (incremental). 5m candles are downloaded only
  when a new 5m candle has closed.
- Duplicate protection: one alert per coin + direction + candle (stored in SQLite), plus a cooldown
  (`ALERT_COOLDOWN_MIN`) and a cap per scan (`MAX_ALERTS_PER_SCAN`).

**1M engine** - 10 candlestick patterns, volume spike (vs previous 20 candles), RSI turn, short-term momentum.
**5M engine** - trend (EMA 20/50 + slope), market structure (higher highs/lows or lower highs/lows), momentum (RSI + MACD histogram).
**5m patterns** - the 10 patterns are also detected on every closed 5m candle (stored in the `patterns` table).
A 5m pattern in the same direction as the 1m signal adds +5 to the score and a reason line.
**Combined** - a 1m pattern becomes an alert only if the **latest closed 5m candle** confirms the trend,
the score is ≥ `MIN_ALERT_SCORE`, and (by default) volume is confirmed.

**Patterns** (`father/patterns.py`): Bullish/Bearish Engulfing, Hammer, Shooting Star, Morning/Evening Star,
Bullish/Bearish Marubozu, Tweezer Bottom/Top. Vectorised pandas, using only current and past candles (tested for look-ahead).

**Engineering score (0-100)**: pattern 30, volume 0-15, 5m trend 0-20, 5m structure 0-10, 5m momentum 0-10,
1m momentum 0-10, RSI room 0-5, 5m pattern in the same direction +5 (max 100). Optional adjustment from your own backtest results (patterns that lost money
out-of-sample get −15). Classes: 85-100 Strong candidate · 75-84 Qualified candidate · 60-74 Watchlist · <60 Reject.

**Backtest** (`father/backtest.py`)
- Every pattern tested separately on 1m, on 5m, and as the combined 1m + 5m strategy.
- Also: long vs short, volume-confirmed vs not, trend-confirmed vs not, market regime (trend up / trend down / range),
  volatility (high / low), score class.
- Entry at the **next candle open** (no look-ahead). Fees (default 0.05%/side) and slippage (0.02%/side) on both sides.
- TP = 1.5×ATR, SL = 1.0×ATR, time stop 30 bars. If TP and SL hit in the same candle, SL is assumed (conservative).
- No overlapping trades per coin inside each tested group.
- Chronological split: 60% train → 20% validation → 20% **test (unseen)**.
  A pattern is "positive after costs" only if train, validation **and** test all have profit factor > 1.
- Fewer than **200 test trades** = `insufficient sample` (do not trust it yet).
- Stats: total, wins, losses, win rate, profit factor, net return, max drawdown, average return per trade,
  expectancy, average win/loss, long vs short, pattern by pattern. Returns are % per trade at 1x, summed.

**Honest first result**: a small real test (5 coins, 3 days, Nov 2026) found **no** pattern that was positive
after fees and slippage, and every sample was too small. Run bigger backtests (e.g. `/backtest 20 14`)
and only trust patterns that stay positive in the unseen test with 200+ trades. Past results do not predict the future.

**Alert tracking** - every alert stores reference levels (Entry = signal candle close, SL / TP = ATR × `SL_ATR_MULT` /
`TP_ATR_MULT`). The bot follows each alert with the same rules as the backtest (entry next candle open, SL first if both
touched in one candle, fees + slippage, time stop `MAX_HOLD_BARS`) and stores the outcome in the `signals` table.
See `/performance` or the Dashboard card in the app. These are measurements, not predictions.

**Backtest reports in Telegram**: summary (overall, qualified strategy, long vs short), pattern table with verdicts,
a conditions table (volume confirmed vs not, trend confirmed vs not, regime, volatility, score class) and the full CSV.

**Database** (`data/father.db`, SQLite): candles, patterns, signals, backtest_results, coin_universe,
performance_stats, failed_signals, scanner_errors, settings. Old candles are pruned after `CANDLE_RETENTION_DAYS`;
signals and results are kept.

**API use**: only public endpoints (`exchangeInfo`, `ticker/24hr`, `ticker/bookTicker`, `klines`, `time`).
Requests are paced, request weight is watched (pauses before the limit), and network errors / HTTP 5xx / 429
are retried with backoff. If Binance is blocked in your area (HTTP 451/403) the bot tells you;
`BINANCE_BASE_URL` can be changed in `.env`.

---

## Reliability: heartbeat, watchdog, logs

- Every network call has a timeout and retries with backoff. The whole download stage has a hard deadline (150 s),
  so one stuck coin can not freeze the scanner.
- The scanner loop and the Telegram loop catch every error and keep running. Errors go to `logs/father.log`,
  the `scanner_errors` table, `/errors`, and (for commands / app actions) straight back to your chat.
- Every command and every Mini App action is written to the log ("Command received", "Mini App action received / finished").
- A heartbeat line is logged every 3 minutes (even when the scanner is OFF) and `logs/heartbeat` is updated every minute.
- `start.sh` is a watchdog: if `logs/heartbeat` is older than 5 minutes on two checks in a row, it dumps all thread stacks
  to `logs/threads.txt` and restarts the bot. It also restarts the bot after a crash.
- If the phone (or computer) sleeps, the bot logs "Process was paused for X min" when it wakes up and resets its network
  connections. A sleeping device can not scan - nothing can fix that in software.
- Hang diagnosis by hand: `kill -USR1 <pid>` writes all thread stacks to `logs/threads.txt`.

## Running on Android - please read

Android can stop background apps to save battery. **24/7 running is not guaranteed.** To help:
- `start.sh` calls `termux-wake-lock` and restarts the bot if it stops.
- Settings → Apps → Termux → Battery → **Unrestricted / Don't optimise**.
- Keep the Termux notification. Some phones (Xiaomi, Oppo, Vivo, Samsung) also need "Autostart" / "Lock app in recents".
- Optional auto-start after reboot: install **Termux:Boot** (F-Droid), open it once, then:
  ```bash
  mkdir -p ~/.termux/boot
  printf '#!/data/data/com.termux/files/usr/bin/sh\ntermux-wake-lock\ncd ~/father-signal-bot && ./start.sh >> logs/boot.log 2>&1\n' > ~/.termux/boot/start-father.sh
  chmod +x ~/.termux/boot/start-father.sh
  ```
- Use `/status` in Telegram to check it is still alive. Logs: `logs/father.log`.

Battery / data: about 60-120 small requests per minute for ~100 coins. Lower `UNIVERSE_SIZE` (e.g. `/set universe_size 30`) to save battery.

Update later: `cd ~/father-signal-bot && git pull`.

---

## Roman Urdu - Quick Start

1. **F-Droid** se **Termux** install karein.
2. Termux mein yeh ek command paste karein:
   ```bash
   pkg install -y git python && git clone https://github.com/ahmadrahmani147370-hype/father-signal-bot && cd father-signal-bot && bash setup_termux.sh
   ```
3. Telegram mein **@BotFather** se `/newbot` karke token lein. **@userinfobot** se apna chat id lein.
4. `nano .env` kholein, `TELEGRAM_BOT_TOKEN` aur `TELEGRAM_CHAT_ID` likhein, save karein (Ctrl+O, Enter, Ctrl+X).
5. `./start.sh` chalayein. Phir Telegram mein bot ko `/help` bhejein.
6. Backtest ke liye: `/backtest 5 3` (top 5 coins, 3 din). Report: `/report`. Band karna: `/scan_off`.
7. **Father App:** bot ko `/app` bhejein. Neeche **📊 Father App** button aayega, us par tap karein.
   Dashboard, Signals, Backtest aur Settings tabs milenge. App ke buttons (Scan On/Off, Backtest, Setting) dabane se
   app band ho jati hai aur bot chat mein jawab deta hai, saath naya 📊 button bhi. Menu button wali app sirf
   dekhne ke liye hai (read-only) - buttons ke liye 📊 keyboard button use karein. Koi server nahi chahiye.

8. `/performance` se purane alerts ka nateeja dekhein (TP pehle laga ya SL, fees ke baad).
9. `./start.sh` watchdog bhi hai: agar bot 5 minute tak heartbeat na de to khud restart kar deta hai.
   Log file: `logs/father.log`. Har 3 minute baad "heartbeat" line aati hai.

**Zaroori baatein:**
- Yeh bot sirf **analysis** karta hai. Koi trade, order ya wallet nahi. Binance API key ki zaroorat nahi.
- "Signal Score" sirf engineering score hai, **jeetne ka chance nahi**.
- 200 se kam test trades wale result par bharosa na karein ("insufficient sample").
- Agar phone/computer so jaye (sleep) to bot bhi ruk jata hai; jagne par log mein "Process was paused" likha aata hai.
- Android background mein app band kar sakta hai, is liye 24/7 ki guarantee nahi. Termux ki battery setting
  "Unrestricted" rakhein aur `/status` se check karte rahein.

---

## Files

```
main.py                    start the bot (Telegram + scanner); --debug CLI
father/config.py           settings from .env, /set tunables
father/binance.py          public REST client (pacing, weight, retries)
father/patterns.py         10 candlestick patterns
father/strategy.py         1m + 5m engines, engineering score
father/scanner.py          coin universe, closed-candle scanning, duplicate checks
father/backtest.py         backtest engine and reports
father/db.py               SQLite storage
father/telegram_alerts.py  Telegram API + signal message format
father/telegram_bot.py     Telegram commands (long polling) + Mini App actions
father/webapp.py           Mini App data link (base64url JSON) + action whitelist
webapp/                    Mini App static page (GitHub Pages): index.html, app.js, style.css
tests/test_core.py         unit tests (synthetic candles)
tests/test_webapp.py       Mini App encoding + action tests
tests/test_robustness.py   alert tracking, 5m patterns, error reporting, loop survival, reports
setup_termux.sh            Termux installer
start.sh                   run with wake-lock + auto-restart
```

Future ideas: Binance WebSocket streams instead of polling, more patterns, walk-forward testing.
