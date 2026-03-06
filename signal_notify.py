"""
SignalNotify - Telegram + MetaTrader 5 Price Signal Monitor

Reads signals like 'GOLD@5000' from Telegram channels, monitors MT5 prices,
and sends notifications back when the target price is reached.
Stops monitoring if the original signal message is deleted.
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, Tuple

import MetaTrader5 as mt5
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import MessageIdInvalidError

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("SignalNotify")

# ── Load config ──────────────────────────────────────────────────────────────
load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
CHANNELS = [
    c.strip() for c in os.getenv("TELEGRAM_CHANNELS", "").split(",") if c.strip()
]

MT5_PATH = os.getenv("MT5_PATH", "")

PRICE_CHECK_INTERVAL = float(os.getenv("PRICE_CHECK_INTERVAL", "1"))
MESSAGE_CHECK_INTERVAL = float(os.getenv("MESSAGE_CHECK_INTERVAL", "30"))
SIGNAL_MAX_AGE = float(os.getenv("SIGNAL_MAX_AGE", str(24 * 60 * 60)))  # 24 hours
EXPIRY_CHECK_INTERVAL = float(os.getenv("EXPIRY_CHECK_INTERVAL", "60"))  # check every minute
RECONNECT_BASE_DELAY = float(os.getenv("RECONNECT_BASE_DELAY", "10"))  # initial delay before reconnect
RECONNECT_MAX_DELAY = float(os.getenv("RECONNECT_MAX_DELAY", "300"))  # max delay between reconnects

# ── Signal pattern ───────────────────────────────────────────────────────────
# Matches patterns like:  GOLD@5000  |  EURUSD@1.08550  |  XAUUSD @ 2050.50
SIGNAL_RE = re.compile(
    r"^([A-Za-z0-9_.]+)\s*@\s*([0-9]+(?:\.[0-9]+)?)\s*$"
)

# ── Symbol aliases ───────────────────────────────────────────────────────────
# Map friendly names to MT5 symbol names (extend as needed)
SYMBOL_ALIASES: Dict[str, str] = {
    "GOLD": "XAUUSD",
    "SILVER": "XAGUSD",
    "OIL": "USOIL",
    "BTC": "BTCUSD",
    "ETH": "ETHUSD",
}


# Pip milestones to notify at (in pips away from target)
PIP_MILESTONES = [30, 20, 10]


@dataclass
class Signal:
    """Represents an active price signal being monitored."""

    symbol: str  # MT5 symbol name
    target_price: float
    channel_id: int  # Telegram chat/channel id
    message_id: int  # Original signal message id
    raw_text: str  # Original message text
    initial_price: Optional[float] = None  # Price when signal was created
    pip_size: float = 0.0001  # Will be set from MT5 symbol info
    notified_milestones: Set[int] = field(default_factory=set)  # Pip milestones already sent
    previous_price: Optional[float] = None  # Last checked price for crossing detection
    created_at: float = field(default_factory=time.time)

    @property
    def key(self) -> Tuple[int, int]:
        """Unique key: (channel_id, message_id)."""
        return (self.channel_id, self.message_id)

    def pips_away(self, current_price: float) -> float:
        """Calculate how many pips the current price is from the target."""
        return abs(current_price - self.target_price) / self.pip_size


class SignalMonitor:
    """Core class that ties Telegram and MT5 together."""

    def __init__(self) -> None:
        self.client: Optional[TelegramClient] = None
        self.active_signals: Dict[Tuple[int, int], Signal] = {}
        self._running = False
        self._resolved_channels: Dict[str, int] = {}
        self._symbol_cache: Dict[str, str] = {}  # requested name → actual MT5 name
        self._mt5_consecutive_failures = 0
        self._mt5_connected = False

    # ── MT5 ──────────────────────────────────────────────────────────────

    @staticmethod
    def init_mt5() -> bool:
        """Initialize connection to MetaTrader 5."""
        kwargs = {}
        if MT5_PATH:
            kwargs["path"] = MT5_PATH
        if not mt5.initialize(**kwargs):
            log.error("MT5 initialize() failed: %s", mt5.last_error())
            return False

        info = mt5.account_info()
        if info:
            log.info(
                "MT5 connected \u2013 Account: %s  Server: %s  Balance: %.2f",
                info.login,
                info.server,
                info.balance,
            )
        return True

    def reconnect_mt5(self) -> bool:
        """Attempt to re-establish the MT5 connection."""
        log.info("Attempting MT5 reconnection\u2026")
        try:
            mt5.shutdown()
        except Exception:
            pass
        if self.init_mt5():
            self._mt5_consecutive_failures = 0
            self._mt5_connected = True
            self._symbol_cache.clear()  # force re-resolve after reconnect
            log.info("MT5 reconnected successfully.")
            return True
        log.warning("MT5 reconnection failed.")
        self._mt5_connected = False
        return False

    def check_mt5_health(self) -> bool:
        """Return True if MT5 is responding, False otherwise."""
        info = mt5.account_info()
        if info is not None:
            if not self._mt5_connected:
                log.info("MT5 connection restored.")
                self._mt5_connected = True
                self._mt5_consecutive_failures = 0
            return True
        self._mt5_connected = False
        return False

    def find_mt5_symbol(self, name: str) -> Optional[str]:
        """Find the actual MT5 symbol name for a given name.

        Tries exact match first, then searches for symbols containing the name.
        Caches results for future lookups.
        """
        if name in self._symbol_cache:
            return self._symbol_cache[name]

        # 1. Exact match
        info = mt5.symbol_info(name)
        if info is not None:
            mt5.symbol_select(name, True)
            self._symbol_cache[name] = name
            log.info("MT5 symbol exact match: %s", name)
            return name

        # 2. Search for symbols containing the name (e.g. XAUUSD → XAUUSDm)
        found = mt5.symbols_get(f"*{name}*")
        if found:
            # Prefer the shortest match (closest to the base name)
            best = min(found, key=lambda s: len(s.name))
            mt5.symbol_select(best.name, True)
            self._symbol_cache[name] = best.name
            log.info(
                "MT5 symbol resolved: %s → %s  (from %d candidates)",
                name,
                best.name,
                len(found),
            )
            return best.name

        log.warning("MT5 symbol not found for '%s'", name)
        return None

    def get_price(self, symbol: str) -> Optional[float]:
        """Return the current bid price for *symbol*, or None on failure."""
        actual = self.find_mt5_symbol(symbol)
        if actual is None:
            return None
        tick = mt5.symbol_info_tick(actual)
        if tick is None:
            return None
        return tick.bid

    def get_pip_size(self, symbol: str) -> float:
        """Return the pip size for a symbol based on MT5 symbol info.

        Standard conventions:
        - Forex majors (5 digits): pip = 0.0001  (e.g. EURUSD 1.08550)
        - JPY pairs (3 digits):    pip = 0.01    (e.g. USDJPY 150.550)
        - Gold / XAUUSD (2 digits): pip = 0.10   (e.g. 5098.75)
        - Silver / XAGUSD (2-3 digits): pip = 0.010
        - Indices: pip = 1.0 or 0.1 depending on instrument
        """
        actual = self.find_mt5_symbol(symbol)
        if actual is None:
            return 0.0001
        info = mt5.symbol_info(actual)
        if info is None:
            return 0.0001

        name_upper = actual.upper()
        digits = info.digits
        point = info.point

        # ── Override for known instrument types ──
        # Gold
        if "XAU" in name_upper or "GOLD" in name_upper:
            return 0.10
        # Silver
        if "XAG" in name_upper or "SILVER" in name_upper:
            return 0.010
        # Oil
        if "OIL" in name_upper or "WTI" in name_upper or "BRENT" in name_upper:
            return 0.01

        # ── Generic forex calculation ──
        # 5-digit / 3-digit brokers: pip = 10 * point
        if digits in (3, 5):
            return point * 10
        return point

    @staticmethod
    def resolve_symbol(name: str) -> str:
        """Resolve a friendly name (e.g. GOLD) to an MT5 symbol."""
        upper = name.upper()
        return SYMBOL_ALIASES.get(upper, upper)

    # ── Telegram ─────────────────────────────────────────────────────────

    async def start_telegram(self) -> None:
        """Create and start the Telethon client."""
        self.client = TelegramClient(
            "signal_notify_session",
            API_ID,
            API_HASH,
            connection_retries=10,
            retry_delay=5,
            auto_reconnect=True,
            request_retries=5,
        )
        await self.client.start()
        me = await self.client.get_me()
        log.info("Telegram logged in as %s (id=%s)", me.first_name, me.id)

        # Resolve channel identifiers to entity IDs
        # First, try numeric IDs directly; for names, search through dialogs
        remaining = {}  # name -> original config string (lowered for matching)
        for ch in CHANNELS:
            if ch.lstrip("-").isdigit():
                try:
                    entity = await self.client.get_entity(int(ch))
                    self._resolved_channels[ch] = entity.id
                    log.info("Resolved channel '%s' → %s", ch, entity.id)
                except Exception as exc:
                    log.warning("Could not resolve channel ID '%s': %s", ch, exc)
            else:
                remaining[ch] = ch.lower().lstrip("@")

        # Match non-numeric channels by scanning dialogs (groups & channels)
        if remaining:
            log.info("Scanning dialogs to resolve: %s", ", ".join(remaining))
            async for dialog in self.client.iter_dialogs():
                entity = dialog.entity
                title = getattr(entity, "title", "") or ""
                username = getattr(entity, "username", "") or ""
                for ch, target in list(remaining.items()):
                    if (
                        username.lower() == target
                        or title.lower() == target
                    ):
                        self._resolved_channels[ch] = dialog.id
                        log.info(
                            "Resolved channel '%s' → %s (%s)",
                            ch,
                            dialog.id,
                            title or username,
                        )
                        del remaining[ch]
                if not remaining:
                    break

            for ch in remaining:
                log.warning(
                    "Could not resolve channel '%s' – make sure you are "
                    "a member and the name/username matches exactly.",
                    ch,
                )

        if not self._resolved_channels:
            log.error("No channels resolved – nothing to monitor.")
            return

        # Register new-message handler
        channel_ids = list(self._resolved_channels.values())

        @self.client.on(events.NewMessage(chats=channel_ids))
        async def on_new_message(event: events.NewMessage.Event) -> None:
            await self._handle_message(event)

        # Register message-deleted handler
        @self.client.on(events.MessageDeleted())
        async def on_deleted(event: events.MessageDeleted.Event) -> None:
            await self._handle_deleted(event)

        log.info("Listening for signals on %d channel(s)…", len(channel_ids))

    async def _handle_message(self, event: events.NewMessage.Event) -> None:
        """Parse incoming messages for signal patterns."""
        text = (event.raw_text or "").strip()
        match = SIGNAL_RE.match(text)
        if not match:
            return

        raw_symbol, raw_price = match.group(1), match.group(2)
        symbol = self.resolve_symbol(raw_symbol)
        target_price = float(raw_price)

        # Get the current price and pip size
        initial_price = self.get_price(symbol)
        pip_size = self.get_pip_size(symbol)

        signal = Signal(
            symbol=symbol,
            target_price=target_price,
            channel_id=event.chat_id,
            message_id=event.id,
            raw_text=text,
            initial_price=initial_price,
            pip_size=pip_size,
        )

        direction = "—"
        if initial_price is not None:
            if initial_price < target_price:
                direction = "▲ waiting for price to rise"
            elif initial_price > target_price:
                direction = "▼ waiting for price to drop"
            else:
                direction = "● already at target"

        self.active_signals[signal.key] = signal
        log.info(
            "NEW SIGNAL: %s @ %.5f  (current: %s, pip=%.5f)  %s  (channel=%s msg=%s)",
            symbol,
            target_price,
            f"{initial_price:.5f}" if initial_price else "N/A",
            pip_size,
            direction,
            event.chat_id,
            event.id,
        )

        # Notify the channel that the signal has been accepted
        pips_away_str = ""
        if initial_price is not None:
            pips_now = abs(initial_price - target_price) / pip_size
            pips_away_str = f"\n**Distance:** {pips_now:.1f} pips"
        accept_text = (
            f"✅ **Signal accepted!**\n\n"
            f"**Symbol:** {symbol}\n"
            f"**Target:** {target_price}\n"
            f"**Current:** {f'{initial_price:.5f}' if initial_price else 'N/A'}\n"
            f"**Status:** {direction}{pips_away_str}"
        )
        try:
            await self.client.send_message(
                event.chat_id, accept_text, parse_mode="md"
            )
        except Exception as exc:
            log.error("Failed to send acceptance notification: %s", exc)

    async def _handle_deleted(self, event: events.MessageDeleted.Event) -> None:
        """Remove signals whose messages were deleted."""
        deleted_ids: Set[int] = set(event.deleted_ids)
        channel_id = getattr(event, "chat_id", None) or getattr(
            event, "channel_id", None
        )
        removed = []
        for key, sig in list(self.active_signals.items()):
            if sig.message_id in deleted_ids:
                if channel_id is None or sig.channel_id == channel_id:
                    removed.append(key)
        for key in removed:
            sig = self.active_signals.pop(key)
            log.info(
                "SIGNAL REMOVED (message deleted): %s @ %.5f  (channel=%s msg=%s)",
                sig.symbol,
                sig.target_price,
                sig.channel_id,
                sig.message_id,
            )

    async def send_hit_notification(self, signal: Signal, current_price: float) -> None:
        """Send a Telegram message to the channel that the target price was hit."""
        if not self.client or not self.client.is_connected():
            return
        text = (
            f"🎯 **PRICE HIT!**\n\n"
            f"**Symbol:** {signal.symbol}\n"
            f"**Target:** {signal.target_price}\n"
            f"**Current:** {current_price}\n"
            f"**Original signal:** `{signal.raw_text}`"
        )
        try:
            await self.client.send_message(signal.channel_id, text, parse_mode="md")
            log.info(
                "Notification sent for %s @ %.5f → channel %s",
                signal.symbol,
                signal.target_price,
                signal.channel_id,
            )
        except Exception as exc:
            log.error("Failed to send notification: %s", exc)

    async def _send_proximity_alert(
        self, signal: Signal, current_price: float, milestone: int, pips: float
    ) -> None:
        """Send a Telegram notification when price is near the target."""
        if not self.client or not self.client.is_connected():
            return
        text = (
            f"📍 **{milestone} pips away!**\n\n"
            f"**Symbol:** {signal.symbol}\n"
            f"**Target:** {signal.target_price}\n"
            f"**Current:** {current_price:.5f}\n"
            f"**Distance:** {pips:.1f} pips"
        )
        try:
            await self.client.send_message(signal.channel_id, text, parse_mode="md")
            log.info(
                "Proximity alert (%d pips) sent for %s → channel %s",
                milestone,
                signal.symbol,
                signal.channel_id,
            )
        except Exception as exc:
            log.error("Failed to send proximity alert: %s", exc)

    # ── Expiry ───────────────────────────────────────────────────────────

    async def _send_expiry_notification(self, signal: Signal) -> None:
        """Send a Telegram message when a signal is removed due to 24h expiry."""
        if not self.client or not self.client.is_connected():
            return
        age_hours = (time.time() - signal.created_at) / 3600
        text = (
            f"⏰ **Signal expired (24h)**\n\n"
            f"**Symbol:** {signal.symbol}\n"
            f"**Target:** {signal.target_price}\n"
            f"**Age:** {age_hours:.1f} hours\n"
            f"**Original signal:** `{signal.raw_text}`"
        )
        try:
            await self.client.send_message(signal.channel_id, text, parse_mode="md")
            log.info(
                "Expiry notification sent for %s @ %.5f → channel %s",
                signal.symbol,
                signal.target_price,
                signal.channel_id,
            )
        except Exception as exc:
            log.error("Failed to send expiry notification: %s", exc)

    # ── Message-still-exists check ───────────────────────────────────────

    async def _verify_messages_exist(self) -> None:
        """Periodically verify that signal messages have not been deleted."""
        if not self.client or not self.client.is_connected():
            return
        to_remove = []
        for key, sig in list(self.active_signals.items()):
            try:
                msgs = await self.client.get_messages(
                    sig.channel_id, ids=sig.message_id
                )
                # get_messages returns None when the message no longer exists
                if msgs is None:
                    to_remove.append(key)
            except MessageIdInvalidError:
                to_remove.append(key)
            except Exception as exc:
                log.debug("Could not verify msg %s: %s", sig.message_id, exc)

        for key in to_remove:
            sig = self.active_signals.pop(key)
            log.info(
                "SIGNAL REMOVED (message no longer exists): %s @ %.5f  "
                "(channel=%s msg=%s)",
                sig.symbol,
                sig.target_price,
                sig.channel_id,
                sig.message_id,
            )

    # ── Main loops ───────────────────────────────────────────────────────

    async def _price_loop(self) -> None:
        """Check MT5 prices against active signals."""
        MT5_FAILURE_THRESHOLD = 5  # consecutive no-data cycles before reconnect
        while self._running:
            # ── MT5 health gate ──
            if not self._mt5_connected or not self.check_mt5_health():
                self._mt5_consecutive_failures += 1
                if self._mt5_consecutive_failures >= MT5_FAILURE_THRESHOLD:
                    self.reconnect_mt5()
                    self._mt5_consecutive_failures = 0
                await asyncio.sleep(PRICE_CHECK_INTERVAL)
                continue

            hit_keys: list[Tuple[Tuple[int, int], float]] = []
            got_any_tick = False

            for key, sig in list(self.active_signals.items()):
                actual_symbol = self.find_mt5_symbol(sig.symbol)
                if actual_symbol is None:
                    log.debug("No MT5 symbol for %s", sig.symbol)
                    continue
                tick = mt5.symbol_info_tick(actual_symbol)
                if tick is None:
                    log.debug("No tick data for %s (%s)", sig.symbol, actual_symbol)
                    continue
                got_any_tick = True

                bid = tick.bid
                target = sig.target_price

                if sig.initial_price is None:
                    # No initial price — set it now and skip this tick
                    sig.initial_price = bid
                    log.info(
                        "Set initial price for %s: %.5f (target %.5f, pip=%.5f)",
                        sig.symbol, bid, target, sig.pip_size,
                    )
                    continue

                # ── Proximity alerts (check BEFORE hit so they fire even
                #    when price jumps past milestones to hit in one tick) ──
                pips = sig.pips_away(bid)
                for milestone in sorted(PIP_MILESTONES, reverse=True):
                    if (
                        pips <= milestone
                        and milestone not in sig.notified_milestones
                    ):
                        sig.notified_milestones.add(milestone)
                        await self._send_proximity_alert(
                            sig, bid, milestone, pips
                        )

                # ── Hit check (crossing detection) ──
                # Only fire when price actually crosses through or touches
                # the target level between ticks, regardless of direction.
                hit = False
                if sig.previous_price is None:
                    # First price check — just set baseline
                    sig.previous_price = bid
                    # Only fire if price is already at target (within 1 pip)
                    if abs(bid - target) <= sig.pip_size:
                        hit = True
                else:
                    lo = min(sig.previous_price, bid)
                    hi = max(sig.previous_price, bid)
                    hit = lo <= target <= hi
                    sig.previous_price = bid

                if hit:
                    log.info(
                        "PRICE HIT detected: %s bid=%.5f target=%.5f",
                        sig.symbol, bid, target,
                    )
                    hit_keys.append((key, bid))

            for key, current_price in hit_keys:
                if key in self.active_signals:
                    sig = self.active_signals.pop(key)
                    await self.send_hit_notification(sig, current_price)

            # Track consecutive cycles with no tick data (possible MT5 disconnect)
            if self.active_signals and not got_any_tick:
                self._mt5_consecutive_failures += 1
                if self._mt5_consecutive_failures >= MT5_FAILURE_THRESHOLD:
                    log.warning("No MT5 tick data for %d cycles, reconnecting\u2026", MT5_FAILURE_THRESHOLD)
                    self.reconnect_mt5()
            else:
                self._mt5_consecutive_failures = 0

            await asyncio.sleep(PRICE_CHECK_INTERVAL)

    async def _message_check_loop(self) -> None:
        """Periodically verify signal messages still exist."""
        while self._running:
            await asyncio.sleep(MESSAGE_CHECK_INTERVAL)
            await self._verify_messages_exist()

    async def _expiry_loop(self) -> None:
        """Remove signals that have been active for more than SIGNAL_MAX_AGE."""
        while self._running:
            await asyncio.sleep(EXPIRY_CHECK_INTERVAL)
            now = time.time()
            expired_keys = [
                key
                for key, sig in self.active_signals.items()
                if now - sig.created_at >= SIGNAL_MAX_AGE
            ]
            for key in expired_keys:
                sig = self.active_signals.pop(key)
                log.info(
                    "SIGNAL EXPIRED (24h): %s @ %.5f  (channel=%s msg=%s)",
                    sig.symbol,
                    sig.target_price,
                    sig.channel_id,
                    sig.message_id,
                )
                await self._send_expiry_notification(sig)

    async def _status_loop(self) -> None:
        """Log active signals count periodically."""
        while self._running:
            await asyncio.sleep(60)
            if self.active_signals:
                log.info("Active signals: %d", len(self.active_signals))
                for sig in self.active_signals.values():
                    price = self.get_price(sig.symbol)
                    log.info(
                        "  • %s @ %.5f  (current: %s)",
                        sig.symbol,
                        sig.target_price,
                        f"{price:.5f}" if price else "N/A",
                    )

    # ── Entry point ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start everything and run until interrupted."""
        # Connect MT5
        if not self.init_mt5():
            log.error("Cannot start without MT5 connection.")
            return
        self._mt5_connected = True

        reconnect_delay = RECONNECT_BASE_DELAY

        try:
            while True:
                # (Re)connect Telegram
                try:
                    await self.start_telegram()
                except Exception as exc:
                    log.error("Telegram connection failed: %s", exc)
                    log.info("Retrying in %.0f seconds…", reconnect_delay)
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX_DELAY)
                    continue

                if not self._resolved_channels:
                    mt5.shutdown()
                    return

                self._running = True
                reconnect_delay = RECONNECT_BASE_DELAY  # reset on success
                log.info("SignalNotify is running. Press Ctrl+C to stop.")

                try:
                    await asyncio.gather(
                        self._price_loop(),
                        self._message_check_loop(),
                        self._expiry_loop(),
                        self._status_loop(),
                        self.client.run_until_disconnected(),
                    )
                except ConnectionError as exc:
                    log.warning("Telegram connection lost: %s", exc)
                except OSError as exc:
                    log.warning("Network error: %s", exc)
                finally:
                    self._running = False
                    if self.client and self.client.is_connected():
                        await self.client.disconnect()

                log.info(
                    "Connection ended. Reconnecting in %.0f seconds…",
                    reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX_DELAY)

        except (KeyboardInterrupt, asyncio.CancelledError):
            log.info("Shutting down…")
        finally:
            self._running = False
            if self.client and self.client.is_connected():
                await self.client.disconnect()
            mt5.shutdown()
            log.info("MT5 disconnected. Bye!")


def main() -> None:
    # Validate essential config
    missing = []
    if not API_ID:
        missing.append("TELEGRAM_API_ID")
    if not API_HASH:
        missing.append("TELEGRAM_API_HASH")
    if not CHANNELS:
        missing.append("TELEGRAM_CHANNELS")
    if missing:
        log.error(
            "Missing required env vars: %s. "
            "Copy .env.example → .env and fill in your values.",
            ", ".join(missing),
        )
        return

    monitor = SignalMonitor()
    asyncio.run(monitor.run())


if __name__ == "__main__":
    main()
