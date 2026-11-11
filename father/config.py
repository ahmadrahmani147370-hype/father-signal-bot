"""Configuration loaded from .env (python-dotenv) with safe defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent
if load_dotenv is not None:
    load_dotenv(ROOT / ".env")


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name: str, default: int) -> int:
    return int(_f(name, default))


def _s(name: str, default: str) -> str:
    v = os.getenv(name)
    return v.strip() if v and v.strip() else default


@dataclass
class Config:
    telegram_token: str = field(default_factory=lambda: _s("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _s("TELEGRAM_CHAT_ID", ""))

    base_url: str = field(default_factory=lambda: _s("BINANCE_BASE_URL", "https://fapi.binance.com").rstrip("/"))
    request_min_interval: float = field(default_factory=lambda: _f("REQUEST_MIN_INTERVAL", 0.12))
    max_weight_per_min: int = field(default_factory=lambda: _i("MAX_WEIGHT_PER_MIN", 1200))
    http_timeout: float = field(default_factory=lambda: _f("HTTP_TIMEOUT", 10))
    http_max_retries: int = field(default_factory=lambda: _i("HTTP_MAX_RETRIES", 5))
    max_workers: int = field(default_factory=lambda: max(1, _i("MAX_WORKERS", 3)))

    universe_size: int = field(default_factory=lambda: _i("UNIVERSE_SIZE", 100))
    universe_refresh_min: float = field(default_factory=lambda: _f("UNIVERSE_REFRESH_MIN", 15))
    min_quote_volume: float = field(default_factory=lambda: _f("MIN_QUOTE_VOLUME_USDT", 20_000_000))
    max_spread_bps: float = field(default_factory=lambda: _f("MAX_SPREAD_BPS", 5))
    min_history: int = field(default_factory=lambda: _i("MIN_HISTORY_CANDLES", 210))
    close_delay_sec: float = field(default_factory=lambda: _f("CANDLE_CLOSE_DELAY_SEC", 3))

    volume_spike_ratio: float = field(default_factory=lambda: _f("VOLUME_SPIKE_RATIO", 1.5))
    ema_fast: int = field(default_factory=lambda: _i("EMA_FAST", 20))
    ema_slow: int = field(default_factory=lambda: _i("EMA_SLOW", 50))
    rsi_period: int = field(default_factory=lambda: _i("RSI_PERIOD", 14))
    atr_period: int = field(default_factory=lambda: _i("ATR_PERIOD", 14))
    min_alert_score: float = field(default_factory=lambda: _f("MIN_ALERT_SCORE", 75))
    require_volume: int = field(default_factory=lambda: _i("REQUIRE_VOLUME_CONFIRMATION", 1))
    max_alerts_per_scan: int = field(default_factory=lambda: _i("MAX_ALERTS_PER_SCAN", 5))
    alert_cooldown_min: float = field(default_factory=lambda: _f("ALERT_COOLDOWN_MIN", 15))
    use_bt_weights: bool = field(default_factory=lambda: _i("USE_BACKTEST_PATTERN_WEIGHTS", 1) == 1)

    fee_pct: float = field(default_factory=lambda: _f("FEE_PCT_PER_SIDE", 0.05))
    slippage_pct: float = field(default_factory=lambda: _f("SLIPPAGE_PCT_PER_SIDE", 0.02))
    tp_atr: float = field(default_factory=lambda: _f("TP_ATR_MULT", 1.5))
    sl_atr: float = field(default_factory=lambda: _f("SL_ATR_MULT", 1.0))
    max_hold_bars: int = field(default_factory=lambda: _i("MAX_HOLD_BARS", 30))
    train_frac: float = field(default_factory=lambda: _f("TRAIN_FRACTION", 0.6))
    val_frac: float = field(default_factory=lambda: _f("VALIDATION_FRACTION", 0.2))
    min_oos_trades: int = field(default_factory=lambda: _i("MIN_OOS_TRADES", 200))

    webapp_url: str = field(default_factory=lambda: _s(
        "WEBAPP_URL", "https://ahmadrahmani147370-hype.github.io/father-signal-bot/webapp/"))
    db_path: str = field(default_factory=lambda: _s("DB_PATH", "data/father.db"))
    retention_days: int = field(default_factory=lambda: _i("CANDLE_RETENTION_DAYS", 30))
    log_level: str = field(default_factory=lambda: _s("LOG_LEVEL", "INFO"))

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else ROOT / p


def load_config() -> Config:
    return Config()


# Tunables that can be changed from Telegram with /set <key> <value>.
# key: (Config attribute, type, min, max, description)
TUNABLES = {
    "min_score": ("min_alert_score", float, 50, 100, "Minimum engineering score to alert"),
    "max_spread_bps": ("max_spread_bps", float, 0.5, 50, "Max bid/ask spread in basis points"),
    "min_volume": ("min_quote_volume", float, 1e6, 1e11, "Min 24h quote volume (USDT)"),
    "volume_spike": ("volume_spike_ratio", float, 1.0, 10, "Volume spike ratio vs 20-candle average"),
    "require_volume": ("require_volume", int, 0, 1, "1 = alert only with a volume spike"),
    "max_alerts": ("max_alerts_per_scan", int, 1, 50, "Max alerts per scan (best scores first)"),
    "cooldown_min": ("alert_cooldown_min", float, 0, 1440, "Minutes before re-alerting same coin+side"),
    "universe_size": ("universe_size", int, 5, 200, "Number of coins to scan"),
    "max_workers": ("max_workers", int, 1, 8, "Parallel downloads"),
    "tp_atr": ("tp_atr", float, 0.2, 10, "Backtest take-profit in ATR"),
    "sl_atr": ("sl_atr", float, 0.2, 10, "Backtest stop-loss in ATR"),
    "max_hold": ("max_hold_bars", int, 2, 500, "Backtest time stop (bars)"),
}


def apply_setting(cfg: Config, key: str, raw: str) -> str:
    """Validate and apply one tunable. Returns the normalised value; raises ValueError."""
    if key not in TUNABLES:
        raise ValueError(f"Unknown setting '{key}'. Use one of: {', '.join(TUNABLES)}")
    attr, typ, lo, hi, _ = TUNABLES[key]
    val = typ(float(raw))
    if not (lo <= val <= hi):
        raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
    setattr(cfg, attr, val)
    return str(val)
