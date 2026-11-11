#!/usr/bin/env bash
# FATHER SIGNAL BOT - Termux setup (Android). Safe to run more than once.
set -e
cd "$(dirname "$0")"

echo "== Updating Termux packages =="
pkg update -y && pkg upgrade -y

echo "== Installing Python, git, numpy and pandas (prebuilt, no compiling) =="
pkg install -y python git python-numpy tur-repo
pkg install -y python-pandas || echo "!! python-pandas from tur-repo failed - will try pip below"

echo "== Installing small Python libraries =="
pip install --upgrade requests python-dotenv
python -c "import pandas" 2>/dev/null || pip install pandas

python -c "import pandas, numpy, requests, dotenv; print('pandas', pandas.__version__, '| numpy', numpy.__version__)"

mkdir -p data logs
if [ ! -f .env ]; then
  cp .env.example .env
  echo "== Created .env - now add your Telegram token and chat id: nano .env =="
fi
chmod +x start.sh
echo
echo "Done. Next:"
echo "  1) nano .env      (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)"
echo "  2) ./start.sh     (starts the bot with wake-lock + auto-restart)"
echo "  3) Open Telegram and send /help to your bot"
