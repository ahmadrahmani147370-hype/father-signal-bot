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

## Telegram commands

| Command | What it does |
|---|---|
| `/start`, `/help` | Show commands |
| `/status` | Scanner on/off, coins, last scan time, errors in last hour, API weight |
| `/scan_on` / `/scan_off` | Start / pause automatic scanning (one check per closed 1m candle) |
| `/scan_once` | Run one scan now |
| `/backtest [symbols] [days]` | e.g. `/backtest 5 3` = top 5 coins, last 3 days. Runs in the background, sends a summary, a per-pattern table and a CSV file |
| `/report` | Latest backtest results per pattern |
| `/signals [n]` | Last n signals |
| `/coins` | Current coin universe (volume, spread) |
| `/settings` | Show settings you can change |
| `/set key value` | e.g. `/set min_score 80`, `/set max_spread_bps 3`, `/set min_volume 50000000` |
| `/errors` | Recent scanner errors |

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
- Filters: minimum 24h volume (`MIN_QUOTE_VOLUME_USDT`), maximum bid/ask spread (`MAX_SPREAD_BPS`),
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
**Combined** - a 1m pattern becomes an alert only if the **latest closed 5m candle** confirms the trend,
the score is ≥ `MIN_ALERT_SCORE`, and (by default) volume is confirmed.

**Patterns** (`father/patterns.py`): Bullish/Bearish Engulfing, Hammer, Shooting Star, Morning/Evening Star,
Bullish/Bearish Marubozu, Tweezer Bottom/Top. Vectorised pandas, using only current and past candles (tested for look-ahead).

**Engineering score (0-100)**: pattern 30, volume 0-15, 5m trend 0-20, 5m structure 0-10, 5m momentum 0-10,
1m momentum 0-10, RSI room 0-5. Optional adjustment from your own backtest results (patterns that lost money
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

**Database** (`data/father.db`, SQLite): candles, patterns, signals, backtest_results, coin_universe,
performance_stats, failed_signals, scanner_errors, settings. Old candles are pruned after `CANDLE_RETENTION_DAYS`;
signals and results are kept.

**API use**: only public endpoints (`exchangeInfo`, `ticker/24hr`, `ticker/bookTicker`, `klines`, `time`).
Requests are paced, request weight is watched (pauses before the limit), and network errors / HTTP 5xx / 429
are retried with backoff. If Binance is blocked in your area (HTTP 451/403) the bot tells you;
`BINANCE_BASE_URL` can be changed in `.env`.

---

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

**Zaroori baatein:**
- Yeh bot sirf **analysis** karta hai. Koi trade, order ya wallet nahi. Binance API key ki zaroorat nahi.
- "Signal Score" sirf engineering score hai, **jeetne ka chance nahi**.
- 200 se kam test trades wale result par bharosa na karein ("insufficient sample").
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
father/telegram_bot.py     Telegram commands (long polling)
tests/test_core.py         unit tests (synthetic candles)
setup_termux.sh            Termux installer
start.sh                   run with wake-lock + auto-restart
```

Future ideas: Binance WebSocket streams instead of polling, more patterns, walk-forward testing.
