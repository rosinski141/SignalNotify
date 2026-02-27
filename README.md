# SignalNotify

Monitors Telegram channels for price signals (e.g. `GOLD@5000`), watches MetaTrader 5 prices in real-time, and sends a notification back to the channel when the target price is reached.

## Features

- Listens to **multiple Telegram channels** simultaneously
- Parses signals in `SYMBOL@PRICE` format (e.g. `GOLD@2050.50`, `EURUSD@1.08500`)
- Resolves friendly names (`GOLD` → `XAUUSD`, `SILVER` → `XAGUSD`, etc.)
- Monitors MT5 bid/ask prices every second
- Sends a notification message when the target price is hit
- **Automatically stops monitoring** if the original signal message is deleted

## Prerequisites

- **Python 3.10+**
- **MetaTrader 5** installed and running on the same machine
- A **Telegram account** with API credentials

## Setup

### 1. Get Telegram API credentials

1. Go to [https://my.telegram.org](https://my.telegram.org)
2. Log in and go to **API development tools**
3. Create an app to get your `API_ID` and `API_HASH`

### 2. Configure environment

```bash
copy .env.example .env
```

Edit `.env` and fill in:

| Variable | Description |
|---|---|
| `TELEGRAM_API_ID` | Your Telegram API ID |
| `TELEGRAM_API_HASH` | Your Telegram API hash |
| `TELEGRAM_CHANNELS` | Comma-separated channel usernames or IDs |
| `MT5_LOGIN` | Your MT5 account number |
| `MT5_PASSWORD` | Your MT5 password |
| `MT5_SERVER` | Your MT5 broker server name |
| `PRICE_CHECK_INTERVAL` | Seconds between price checks (default: 1) |
| `MESSAGE_CHECK_INTERVAL` | Seconds between message-exists checks (default: 30) |

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run

```bash
python signal_notify.py
```

On first run, Telethon will ask you to authenticate with your phone number. A session file (`signal_notify_session.session`) is saved so you don't need to re-authenticate.

## Signal Format

Send a message in any monitored channel:

```
GOLD@2050.50
EURUSD@1.08500
BTCUSD@65000
```

The script will:
1. Parse the symbol and target price
2. Resolve aliases (GOLD → XAUUSD)
3. Monitor the MT5 price feed
4. Send a notification when price is reached
5. Stop monitoring if the message is deleted

## Symbol Aliases

| Alias | MT5 Symbol |
|-------|-----------|
| GOLD | XAUUSD |
| SILVER | XAGUSD |
| OIL | USOIL |
| BTC | BTCUSD |
| ETH | ETHUSD |

Add more in the `SYMBOL_ALIASES` dict in `signal_notify.py`.
