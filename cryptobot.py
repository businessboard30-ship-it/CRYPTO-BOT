"""
🚀 CryptoBot — Professional Crypto Analysis Bot
- Real market data posts (trending coins, top gainers/losers)
- Live cryptocurrency news (multi-source with fallback)
- Generated charts and graphs
- Price lookups with full details
- Premium tier GHS 50/month
- Auto-posts every 3 minutes
"""

import os
import json
import logging
import random
import io
import re
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple

import httpx
import yfinance as yf
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from PIL import Image

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application, ApplicationBuilder, ContextTypes, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters, ChatMemberHandler
)

logging.basicConfig(level=logging.INFO)

# ════════════════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════════════════

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
PAYSTACK_SECRET = os.environ.get("PAYSTACK_SECRET")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

DATA_DIR = "/data"
os.makedirs(DATA_DIR, exist_ok=True)

USERS_FILE = os.path.join(DATA_DIR, "users.json")
CHANNELS_FILE = os.path.join(DATA_DIR, "channels.json")
LEADERBOARD_FILE = os.path.join(DATA_DIR, "leaderboard.json")

# ════════════════════════════════════════════════════════════════════════════════
# DATA FUNCTIONS
# ════════════════════════════════════════════════════════════════════════════════

def load_json(path):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

def get_user(uid):
    users = load_json(USERS_FILE)
    key = str(uid)
    if key not in users:
        users[key] = {"uid": uid, "premium": False, "premium_until": None, "joined": date.today().isoformat()}
        save_json(USERS_FILE, users)
    return users[key]

def save_user(u):
    users = load_json(USERS_FILE)
    users[str(u["uid"])] = u
    save_json(USERS_FILE, users)

def is_premium(uid):
    if uid == ADMIN_ID:
        return True
    u = get_user(uid)
    if not u.get("premium_until"):
        return False
    return datetime.fromisoformat(u["premium_until"]) > datetime.now()

def register_channel(chat_id, chat_title, chat_type):
    channels = load_json(CHANNELS_FILE)
    channels[str(chat_id)] = {"id": chat_id, "title": chat_title, "type": chat_type}
    save_json(CHANNELS_FILE, channels)

def get_channels():
    return list(load_json(CHANNELS_FILE).keys())

def add_points(uid, name, pts):
    lb = load_json(LEADERBOARD_FILE)
    key = str(uid)
    lb.setdefault(key, {"name": name, "points": 0})
    lb[key]["points"] += pts
    save_json(LEADERBOARD_FILE, lb)

# ════════════════════════════════════════════════════════════════════════════════
# CRYPTO DATA API
# ════════════════════════════════════════════════════════════════════════════════

async def get_trending_coins():
    """Get top trending coins from CoinGecko"""
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get("https://api.coingecko.com/api/v3/search/trending", timeout=10)
            data = r.json()
            return data.get("coins", [])[:5]
    except:
        return []

async def get_top_gainers_losers():
    """Get top gainers and losers"""
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "sparkline": False},
                timeout=10
            )
            data = r.json()
            gainers = sorted(data, key=lambda x: x.get("price_change_percentage_24h", 0) or 0, reverse=True)[:5]
            losers = sorted(data, key=lambda x: x.get("price_change_percentage_24h", 0) or 0)[:5]
            return gainers, losers
    except:
        return [], []

async def get_crypto_price(symbol: str) -> Dict:
    """Get detailed crypto price"""
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": symbol.lower(), "vs_currencies": "usd,ghs", "include_24hr_change": "true", "include_market_cap": "true"},
                timeout=10
            )
            data = r.json()
            if symbol.lower() in data:
                coin = data[symbol.lower()]
                return {
                    "symbol": symbol.upper(),
                    "usd": coin.get("usd", 0),
                    "ghs": coin.get("ghs", 0),
                    "change_24h": coin.get("usd_24h_change", 0),
                    "market_cap": coin.get("usd_market_cap", 0)
                }
    except:
        pass
    return None


def _strip_html(text: str) -> str:
    """Remove HTML tags/entities from RSS description text."""
    text = re.sub(r"<[^>]+>", "", text or "")
    text = text.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    return text.strip()


def safe_md(text: str) -> str:
    """Escape characters that break Telegram's legacy Markdown parser.
    Coin names and especially news headlines from external sources often
    contain *, _, `, or [ which otherwise cause send_message to fail with
    'can't parse entities' and silently drop the whole post.
    """
    if text is None:
        return ""
    text = str(text)
    for ch in ("*", "_", "`", "["):
        text = text.replace(ch, "")
    return text


async def _fetch_rss_titles(client: httpx.AsyncClient, url: str, source_name: str, limit: int = 6) -> List[Dict]:
    """Generic RSS/Atom feed title fetcher. Returns [] on any failure."""
    out = []
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return out
        root = ET.fromstring(r.content)
        # Standard RSS 2.0: channel/item/title
        items = root.findall(".//item")
        if not items:
            # Atom fallback: feed/entry/title
            items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for item in items[:limit]:
            title_el = item.find("title")
            if title_el is None:
                title_el = item.find("{http://www.w3.org/2005/Atom}title")
            if title_el is not None and title_el.text:
                out.append({"title": _strip_html(title_el.text)[:140], "source": source_name})
    except Exception as e:
        logging.warning(f"{source_name} RSS failed: {e}")
    return out


async def get_crypto_news():
    """Get crypto news from multiple sources, falling back if one fails.
    Tries sources in order and stops once we have enough headlines.
    Returns a list of dicts: {"title": str, "source": str}
    """
    results: List[Dict] = []

    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "cryptobot/1.0"}) as c:

        # Source 1: CryptoCompare news (free, no key required, generous rate limit)
        try:
            r = await c.get("https://min-api.cryptocompare.com/data/v2/news/?lang=EN")
            if r.status_code == 200:
                data = r.json()
                for item in data.get("Data", [])[:6]:
                    title = item.get("title")
                    if title:
                        results.append({
                            "title": title.strip(),
                            "source": item.get("source_info", {}).get("name") or item.get("source", "CryptoCompare")
                        })
        except Exception as e:
            logging.warning(f"CryptoCompare news failed: {e}")

        if len(results) >= 3:
            return results[:5]

        # Source 2: CoinDesk RSS
        results += await _fetch_rss_titles(c, "https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk")
        if len(results) >= 3:
            return results[:5]

        # Source 3: CoinTelegraph RSS
        results += await _fetch_rss_titles(c, "https://cointelegraph.com/rss", "Cointelegraph")
        if len(results) >= 3:
            return results[:5]

        # Source 4: Bitcoin Magazine RSS
        results += await _fetch_rss_titles(c, "https://bitcoinmagazine.com/feed", "Bitcoin Magazine")
        if len(results) >= 3:
            return results[:5]

        # Source 5: Google News RSS search for "cryptocurrency" — this is essentially
        # Google's own crawled/aggregated headlines, very rarely down or rate-limited.
        results += await _fetch_rss_titles(
            c,
            "https://news.google.com/rss/search?q=cryptocurrency+when:1d&hl=en-US&gl=US&ceid=US:en",
            "Google News"
        )
        if len(results) >= 3:
            return results[:5]

        # Source 6: CoinGecko status updates (free, no key required)
        try:
            r = await c.get("https://api.coingecko.com/api/v3/status_updates", params={"per_page": 6})
            if r.status_code == 200:
                data = r.json()
                for item in data.get("status_updates", [])[:6]:
                    desc = item.get("description")
                    proj = (item.get("project") or {}).get("name", "CoinGecko")
                    if desc:
                        title = desc.strip().split("\n")[0][:100]
                        results.append({"title": title, "source": proj})
        except Exception as e:
            logging.warning(f"CoinGecko status_updates failed: {e}")

        if len(results) >= 3:
            return results[:5]

        # Source 6: Reddit r/CryptoCurrency hot posts (free, no key required)
        try:
            r = await c.get("https://www.reddit.com/r/CryptoCurrency/hot.json?limit=6")
            if r.status_code == 200:
                data = r.json()
                for item in data.get("data", {}).get("children", [])[:6]:
                    title = item.get("data", {}).get("title")
                    if title:
                        results.append({"title": title.strip(), "source": "r/CryptoCurrency"})
        except Exception as e:
            logging.warning(f"Reddit news failed: {e}")

    return results[:5]

# ════════════════════════════════════════════════════════════════════════════════
# CHART GENERATION
# ════════════════════════════════════════════════════════════════════════════════

async def generate_price_chart(ticker: str, period: str = "1mo") -> Optional[str]:
    """Generate candlestick chart"""
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period)
        if hist.empty:
            return None

        fig, ax = plt.subplots(figsize=(12, 6), facecolor='#1a1a1a')
        ax.set_facecolor('#2d2d2d')

        width = 0.6
        for i, (idx, row) in enumerate(hist.iterrows()):
            open_p, high, low, close = row['Open'], row['High'], row['Low'], row['Close']
            color = '#00ff00' if close >= open_p else '#ff0000'
            ax.plot([i, i], [low, high], color=color, linewidth=1)
            rect_height = abs(close - open_p)
            rect_y = min(open_p, close)
            from matplotlib.patches import Rectangle
            rect = Rectangle((i - width/2, rect_y), width, rect_height, facecolor=color, edgecolor=color)
            ax.add_patch(rect)

        ax.set_xlim(-1, len(hist))
        ax.set_ylim(hist['Low'].min() * 0.95, hist['High'].max() * 1.05)
        ax.set_title(f"{ticker.upper()} — {period}", color='white', fontsize=14, fontweight='bold')
        ax.tick_params(colors='white')
        ax.grid(True, alpha=0.2)

        path = f"/tmp/chart_{ticker}.png"
        plt.savefig(path, bbox_inches='tight', facecolor='#1a1a1a', dpi=100)
        plt.close()
        return path
    except:
        return None

# ════════════════════════════════════════════════════════════════════════════════
# COMMAND HANDLERS
# ════════════════════════════════════════════════════════════════════════════════

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    get_user(uid)
    await update.message.reply_text(
        "🚀 *CryptoBot — Market Intelligence*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Real-time crypto analysis, trending coins, news & ratings.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 GET PRICE", callback_data="m_price")],
            [InlineKeyboardButton("📊 TRENDING", callback_data="m_trending")],
            [InlineKeyboardButton("📈 TOP GAINERS", callback_data="m_gainers")],
            [InlineKeyboardButton("📉 TOP LOSERS", callback_data="m_losers")],
            [InlineKeyboardButton("📰 NEWS", callback_data="m_news")],
            [InlineKeyboardButton("👑 PREMIUM", callback_data="m_premium")],
        ]),
        parse_mode="Markdown"
    )

# ════════════════════════════════════════════════════════════════════════════════
# CALLBACK HANDLER
# ════════════════════════════════════════════════════════════════════════════════

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    uid = update.effective_user.id
    data = query.data

    # GET PRICE
    if data == "m_price":
        await query.edit_message_text(
            "💰 *GET PRICE*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Type a ticker: bitcoin, ethereum, cardano, etc",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ BACK", callback_data="home")]]),
            parse_mode="Markdown"
        )
        ctx.user_data["mode"] = "price"

    # TRENDING
    elif data == "m_trending":
        await query.edit_message_text("⏳ Loading trending coins...")
        trending = await get_trending_coins()
        if trending:
            msg = "🔥 *TRENDING COINS*\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            for i, coin in enumerate(trending, 1):
                name = coin.get("item", {}).get("name", "Unknown")
                symbol = coin.get("item", {}).get("symbol", "???").upper()
                msg += f"{i}. *{safe_md(name)}* ({safe_md(symbol)})\n"
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ BACK", callback_data="home")]]))
        else:
            await query.edit_message_text("❌ Could not fetch trending coins", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ BACK", callback_data="home")]]))

    # TOP GAINERS
    elif data == "m_gainers":
        await query.edit_message_text("⏳ Loading top gainers...")
        gainers, _ = await get_top_gainers_losers()
        if gainers:
            msg = "📈 *TOP GAINERS (24h)*\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            for coin in gainers[:5]:
                name = safe_md(coin.get("name", "Unknown"))
                change = coin.get("price_change_percentage_24h", 0)
                price = coin.get("current_price", 0)
                msg += f"*{name}* — ${price:,.2f} ({change:+.2f}%)\n"
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ BACK", callback_data="home")]]))
        else:
            await query.edit_message_text("❌ Could not fetch gainers", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ BACK", callback_data="home")]]))

    # TOP LOSERS
    elif data == "m_losers":
        await query.edit_message_text("⏳ Loading top losers...")
        _, losers = await get_top_gainers_losers()
        if losers:
            msg = "📉 *TOP LOSERS (24h)*\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            for coin in losers[:5]:
                name = safe_md(coin.get("name", "Unknown"))
                change = coin.get("price_change_percentage_24h", 0)
                price = coin.get("current_price", 0)
                msg += f"*{name}* — ${price:,.2f} ({change:+.2f}%)\n"
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ BACK", callback_data="home")]]))
        else:
            await query.edit_message_text("❌ Could not fetch losers", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ BACK", callback_data="home")]]))

    # NEWS
    elif data == "m_news":
        await query.edit_message_text("⏳ Loading crypto news...")
        news = await get_crypto_news()
        if news:
            msg = "📰 *CRYPTO NEWS*\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            for article in news[:5]:
                title = article.get("title", "No title")
                source = article.get("source", "")
                if len(title) > 80:
                    title = title[:80] + "…"
                msg += f"• {safe_md(title)}\n  _({safe_md(source)})_\n"
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ BACK", callback_data="home")]]))
        else:
            await query.edit_message_text(
                "❌ Couldn't reach any news source right now (all providers failed). Try again shortly.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ BACK", callback_data="home")]])
            )

    # PREMIUM
    elif data == "m_premium":
        await query.edit_message_text(
            "👑 *PREMIUM — GHS 50/month*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Early access (24h)\n"
            "✅ Advanced charts\n"
            "✅ Priority alerts\n",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ BACK", callback_data="home")]]),
            parse_mode="Markdown"
        )

    # HOME
    elif data == "home":
        await query.edit_message_text(
            "🚀 *CryptoBot — Market Intelligence*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Real-time crypto analysis, trending coins, news & ratings.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 GET PRICE", callback_data="m_price")],
                [InlineKeyboardButton("📊 TRENDING", callback_data="m_trending")],
                [InlineKeyboardButton("📈 TOP GAINERS", callback_data="m_gainers")],
                [InlineKeyboardButton("📉 TOP LOSERS", callback_data="m_losers")],
                [InlineKeyboardButton("📰 NEWS", callback_data="m_news")],
                [InlineKeyboardButton("👑 PREMIUM", callback_data="m_premium")],
            ]),
            parse_mode="Markdown"
        )

# ════════════════════════════════════════════════════════════════════════════════
# MESSAGE HANDLER
# ════════════════════════════════════════════════════════════════════════════════

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text
    mode = ctx.user_data.get("mode")

    if mode == "price":
        data = await get_crypto_price(text)
        if data:
            msg = f"💰 *{safe_md(data['symbol'])}*\n"
            msg += f"Price: ${data['usd']:,.2f}\n"
            msg += f"GHS: ₵{data['ghs']:,.2f}\n"
            msg += f"24h Change: {data['change_24h']:+.2f}%\n"
            msg += f"Market Cap: ${data['market_cap']:,.0f}\n"
            msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            msg += "⚠️ Educational data only"
            await update.message.reply_text(msg, parse_mode="Markdown")
            ctx.user_data["mode"] = None
        else:
            await update.message.reply_text("❌ Coin not found. Try: bitcoin, ethereum, cardano")

# ════════════════════════════════════════════════════════════════════════════════
# AUTO-POST JOB (Every 3 minutes)
# ════════════════════════════════════════════════════════════════════════════════

async def _build_trending_post() -> Optional[str]:
    trending = await get_trending_coins()
    if not trending:
        return None
    msg = "🔥 *TRENDING COINS*\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    for i, coin in enumerate(trending[:3], 1):
        name = safe_md(coin.get("item", {}).get("name", "Unknown"))
        symbol = safe_md(coin.get("item", {}).get("symbol", "???").upper())
        msg += f"{i}. *{name}* ({symbol})\n"
    return msg + "\n⏰ " + datetime.now().strftime("%H:%M UTC")


async def _build_gainers_post() -> Optional[str]:
    gainers, _ = await get_top_gainers_losers()
    if not gainers:
        return None
    msg = "📈 *TOP GAINERS (24h)*\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    for coin in gainers[:3]:
        name = safe_md(coin.get("name", "Unknown"))
        change = coin.get("price_change_percentage_24h", 0)
        price = coin.get("current_price", 0)
        msg += f"*{name}* → ${price:,.2f} ({change:+.2f}%)\n"
    return msg + "\n⏰ " + datetime.now().strftime("%H:%M UTC")


async def _build_losers_post() -> Optional[str]:
    _, losers = await get_top_gainers_losers()
    if not losers:
        return None
    msg = "📉 *TOP LOSERS (24h)*\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    for coin in losers[:3]:
        name = safe_md(coin.get("name", "Unknown"))
        change = coin.get("price_change_percentage_24h", 0)
        price = coin.get("current_price", 0)
        msg += f"*{name}* → ${price:,.2f} ({change:+.2f}%)\n"
    return msg + "\n⏰ " + datetime.now().strftime("%H:%M UTC")


async def _build_news_post() -> Optional[str]:
    news = await get_crypto_news()
    if not news:
        return None
    msg = "📰 *CRYPTO NEWS*\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    for article in news[:3]:
        title = article.get("title", "")
        if len(title) > 70:
            title = title[:70] + "…"
        msg += f"• {safe_md(title)}\n"
    return msg + "\n⏰ " + datetime.now().strftime("%H:%M UTC")


async def auto_post_job(ctx: ContextTypes.DEFAULT_TYPE):
    logging.info("auto_post_job: tick")
    channels = get_channels()
    if not channels:
        logging.warning("auto_post_job: no registered channels, skipping")
        return

    # Try categories in a random order, but fall through to the next one
    # if a given category has no data, instead of giving up on the whole cycle.
    builders = {
        "trending": _build_trending_post,
        "gainers": _build_gainers_post,
        "losers": _build_losers_post,
        "news": _build_news_post,
    }
    order = list(builders.keys())
    random.shuffle(order)

    msg = None
    used_type = None
    for post_type in order:
        try:
            msg = await builders[post_type]()
        except Exception as e:
            logging.error(f"auto_post_job: error building post ({post_type}): {e}")
            msg = None
        if msg:
            used_type = post_type
            break
        logging.warning(f"auto_post_job: no data for post_type={post_type}, trying next category")

    if not msg:
        logging.error("auto_post_job: ALL categories returned no data this cycle (likely a network/egress issue)")
        return

    logging.info(f"auto_post_job: posting type={used_type} to {len(channels)} channel(s)")

    # Send to all channels
    for cid in channels:
        try:
            await ctx.bot.send_message(int(cid), msg, parse_mode="Markdown")
            logging.info(f"auto_post_job: successfully posted to chat_id={cid}")
        except Exception as e:
            logging.error(f"auto_post_job: send FAILED for chat_id={cid}: {e}")

# ════════════════════════════════════════════════════════════════════════════════
# CHAT MEMBERSHIP
# ════════════════════════════════════════════════════════════════════════════════

async def track_chat_membership(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cmu = update.my_chat_member
    if not cmu:
        return
    chat = cmu.chat
    status = cmu.new_chat_member.status
    if status in ("administrator", "creator"):
        register_channel(chat.id, chat.title or chat.username, chat.type)
        logging.info(f"Registered channel: {chat.id} ({chat.title})")

# ════════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════════

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(ChatMemberHandler(track_chat_membership, ChatMemberHandler.MY_CHAT_MEMBER))

    if app.job_queue is None:
        # This happens if `python-telegram-bot[job-queue]` extra isn't installed.
        # Without it, scheduled jobs (including auto_post_job) silently never run.
        logging.error(
            "JobQueue is not available! Install with: "
            "pip install \"python-telegram-bot[job-queue]\". "
            "Auto-posting will NOT work until this is fixed."
        )
    else:
        # `first=10` makes it post ~10s after startup instead of waiting
        # a full 3 minutes for the first post, so you can verify it's alive.
        app.job_queue.run_repeating(auto_post_job, interval=timedelta(minutes=3), first=10)

    logging.info("🚀 CryptoBot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
