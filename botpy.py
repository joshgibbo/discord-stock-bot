"""
Stock Research Bot — a Discord bot for new investors.

What it does (for ANY ticker, not one person's portfolio):
  /research [TICKER]   Full research breakdown for any stock
  /price [TICKER]      Current price of any stock
  /valuation [TICKER]  Valuation metrics for any stock
  /earnings_result [TICKER]  Most recent earnings report
  /earnings [tickers]  Next earnings dates (optional ticker list,
                        otherwise the tickers in watchlist.json)
  /post_week_ahead     Earnings coming up in the next 7 days
  /paper_start, /paper_buy, /paper_sell, /paper_portfolio
                        Simulated (paper) trading — fake money only
  /institutional_list [TICKER]  Top institutional holders (Yahoo data)
  /check_insider_alerts [TICKER]  Recent insider buys (Yahoo data)
  /help                List all commands

Data: Yahoo Finance, via the free yfinance library (no API key needed).
If Yahoo does not return a field, the bot says "n/a" — it never invents
a number. If a ticker does not exist, it says so.

Scheduled posts (only if NEWS_CHANNEL_ID is set in .env):
  * 09:00 Europe/London, Mon-Fri — daily market briefing
  * 14:30 Europe/London, Mon-Fri — US market open update

Setup is in README.md. Never put your real bot token in this file —
put it in a .env file (see .env.example).

Educational only — not financial advice.
"""

import asyncio
import json
import os
import re
import sys
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
import yfinance as yf
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------- config ---

BASE_DIR = Path(__file__).resolve().parent
PAPER_FILE = BASE_DIR / "paper_portfolio.json"
WATCHLIST_FILE = BASE_DIR / "watchlist.json"

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
NEWS_CHANNEL_ID = os.getenv("NEWS_CHANNEL_ID", "").strip()
# Comma-separated tickers for the daily briefing, e.g. "^GSPC,^FTSE,^IXIC,AAPL"
WATCHLIST_ENV = os.getenv("WATCHLIST", "").strip()

LONDON = ZoneInfo("Europe/London")
DISCLAIMER = "Educational only — not financial advice."
PAPER_DISCLAIMER = "Simulated trading, not financial advice."

# Default briefing tickers: major indices only. Edit WATCHLIST in .env
# or watchlist.json to use your own list — nothing here is a personal
# portfolio, and the bot never stores one.
DEFAULT_BRIEFING_TICKERS = ["^GSPC", "^FTSE", "^IXIC", "^DJI"]


def load_watchlist():
    """Tickers for /earnings and /post_week_ahead when no tickers are given.

    Edit watchlist.json to change them. Falls back to the WATCHLIST
    env var, then to a small example list.
    """
    if WATCHLIST_ENV:
        return [t.strip().upper() for t in WATCHLIST_ENV.split(",") if t.strip()]
    try:
        data = json.loads(WATCHLIST_FILE.read_text())
        if isinstance(data, list) and data:
            return [str(t).strip().upper() for t in data if str(t).strip()]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL"]


# ------------------------------------------------------------- formatting ---

def clean_ticker(raw: str) -> str:
    """Normalise a ticker: upper-case, no spaces. Reject anything odd."""
    ticker = (raw or "").strip().upper().replace(" ", "")
    if not ticker or not re.fullmatch(r"[A-Z0-9.\-^]{1,12}", ticker):
        return ""
    return ticker


def fmt_money(value, decimals=2):
    if value is None:
        return "n/a"
    try:
        return f"${float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "n/a"


def fmt_large(value):
    """$1,510,000,000,000 -> $1.51T, $2,400,000,000 -> $2.40B, etc."""
    if value is None:
        return "n/a"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "n/a"
    for suffix, size in [("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)]:
        if abs(value) >= size:
            return f"${value / size:,.2f}{suffix}"
    return f"${value:,.2f}"


def fmt_number(value, decimals=2):
    if value is None:
        return "n/a"
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "n/a"


def fmt_pct(value, decimals=2):
    """For fields Yahoo returns as a fraction, e.g. revenueGrowth
    0.261 (= 26.1%). Multiply by 100."""
    if value is None:
        return "n/a"
    try:
        return f"{float(value) * 100:.{decimals}f}%"
    except (TypeError, ValueError):
        return "n/a"


def fmt_yield(value, decimals=2):
    """Dividend yield: yfinance 1.8 returns this ALREADY as a percent
    (META: 0.36 means 0.36%), unlike revenueGrowth which is a fraction.
    So do NOT multiply by 100 here."""
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{decimals}f}%"
    except (TypeError, ValueError):
        return "n/a"


def fmt_ratio(value):
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.1f}x"
    except (TypeError, ValueError):
        return "n/a"


def fmt_date(value):
    """Accept a date, datetime, Timestamp, or '2027-01-27' string."""
    if value is None:
        return "n/a"
    if isinstance(value, str):
        return value[:10]
    try:
        return value.strftime("%b %d, %Y")
    except AttributeError:
        return str(value)


def _first(info, *keys):
    """First non-None value from info for any of the given keys."""
    for key in keys:
        value = info.get(key)
        if value is not None and value != "":
            return value
    return None


# ------------------------------------------------------------ data layer ----
# These functions are plain (synchronous) Python. The Discord commands
# run them in a thread (asyncio.to_thread) so they never block the bot.

def fetch_stock(ticker: str):
    """Fetch everything /research needs for one ticker.

    Returns a dict, or None if the ticker does not exist / has no price.
    Missing fields are None (shown as "n/a"), never invented.
    """
    tk = yf.Ticker(ticker)

    # --- info dict (company profile, valuation, etc.) ---
    info = {}
    try:
        info = tk.info or {}
    except Exception:
        info = {}

    # --- price: info first, then fast_info, then recent history ---
    price = _first(info, "currentPrice", "regularMarketPrice")
    prev_close = _first(info, "previousClose", "regularMarketPreviousClose")
    if price is None:
        try:
            price = tk.fast_info.get("last_price")
            prev_close = prev_close or tk.fast_info.get("previous_close")
        except Exception:
            pass
    if price is None:
        try:
            hist = tk.history(period="5d")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
                if len(hist) > 1:
                    prev_close = prev_close or float(hist["Close"].iloc[-2])
        except Exception:
            pass
    if price is None:
        return None  # No price at all: almost certainly a bad ticker.

    change_pct = None
    if prev_close:
        try:
            change_pct = (float(price) - float(prev_close)) / float(prev_close) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            change_pct = None

    # --- next earnings date ---
    next_earnings = _first(info, "earningsDate")
    if isinstance(next_earnings, (list, tuple)) and next_earnings:
        next_earnings = next_earnings[0]
    if next_earnings is None:
        try:
            dates = tk.get_earnings_dates(limit=6)
            if dates is not None and not dates.empty:
                # Note: Yahoo returns these newest-first, so take the
                # earliest date that is still in the future.
                future = [d for d in dates.index if d.date() >= date.today()]
                if future:
                    next_earnings = min(future)
        except Exception:
            pass

    summary = _first(info, "longBusinessSummary", "shortBusinessSummary")
    if summary and len(summary) > 800:
        summary = summary[:800].rstrip() + "..."

    return {
        "ticker": ticker,
        "name": _first(info, "longName", "shortName") or ticker,
        "summary": summary,
        "price": price,
        "prev_close": prev_close,
        "change_pct": change_pct,
        "currency": _first(info, "currency") or "USD",
        "market_cap": _first(info, "marketCap"),
        "sector": _first(info, "sector"),
        "industry": _first(info, "industry"),
        "pe": _first(info, "trailingPE"),
        "forward_pe": _first(info, "forwardPE"),
        "pb": _first(info, "priceToBook"),
        "peg": _first(info, "pegRatio"),
        "ev_ebitda": _first(info, "enterpriseToEbitda"),
        "ps": _first(info, "priceToSalesTrailing12Months"),
        "dividend_yield": _first(info, "dividendYield"),
        "low_52w": _first(info, "fiftyTwoWeekLow"),
        "high_52w": _first(info, "fiftyTwoWeekHigh"),
        "eps": _first(info, "trailingEps"),
        "revenue_growth": _first(info, "revenueGrowth"),
        "free_cash_flow": _first(info, "freeCashflow"),
        "target": _first(info, "targetMeanPrice", "targetMedianPrice",
                          "analystPriceTarget"),
        "next_earnings": next_earnings,
        "day_low": _first(info, "dayLow"),
        "day_high": _first(info, "dayHigh"),
        "volume": _first(info, "volume"),
    }


def fetch_news(ticker: str, limit: int = 5):
    """Latest headlines for a ticker. Returns a list of (title, url)."""
    try:
        items = yf.Ticker(ticker).news or []
    except Exception:
        return []
    out = []
    for item in items[:limit]:
        # yfinance 0.2.x nests most fields under "content"; older
        # versions return them flat. Handle both.
        content = item.get("content", {}) if isinstance(item, dict) else {}
        title = content.get("title") or item.get("title")
        url = (
            content.get("clickThroughUrl", {}).get("url")
            if isinstance(content.get("clickThroughUrl"), dict)
            else content.get("clickThroughUrl")
        ) or item.get("link") or item.get("url")
        if title:
            out.append((title, url or ""))
    return out


def fetch_earnings_dates(tickers):
    """Next earnings date for each ticker. Returns {ticker: date-or-None}."""
    results = {}
    for ticker in tickers:
        stock = fetch_stock(ticker)
        results[ticker] = stock["next_earnings"] if stock else None
    return results


def fetch_latest_earnings(ticker: str):
    """Most recent reported earnings. Returns a dict, or None."""
    tk = yf.Ticker(ticker)
    try:
        dates = tk.get_earnings_dates(limit=4)
    except Exception:
        dates = None
    if dates is not None and not dates.empty:
        # Rows are earnings dates; the most recent past one is the
        # latest report. Yahoo returns them newest-first, so use max(),
        # not the last row. Columns: "EPS Estimate", "Reported EPS",
        # "Surprise(%)" (names vary slightly by version).
        past = [d for d in dates.index if d.date() <= date.today()]
        if past:
            latest = max(past)
            row = dates.loc[latest]
            def _col(*names):
                for name in names:
                    try:
                        v = row.get(name)
                        if v is not None and str(v) != "nan":
                            return v
                    except Exception:
                        continue
                return None
            return {
                "date": latest,
                "reported_eps": _col("Reported EPS"),
                "estimated_eps": _col("EPS Estimate", "Estimated EPS"),
                "surprise_pct": _col("Surprise(%)", "Surprise %"),
            }
    # Fallback: quarterly income statement (revenue / net income)
    try:
        q = tk.quarterly_financials
        if q is not None and not q.empty:
            latest = q.columns[0]
            def _row(*names):
                for name in names:
                    if name in q.index:
                        v = q.loc[name, latest]
                        return None if str(v) == "nan" else v
                return None
            return {
                "date": latest,
                "reported_eps": None,
                "estimated_eps": None,
                "surprise_pct": None,
                "revenue": _row("Total Revenue"),
                "net_income": _row("Net Income"),
            }
    except Exception:
        pass
    return None


def fetch_institutional(ticker: str):
    """Top institutional holders from Yahoo. Returns (rows, error).

    rows: list of (holder, shares, % out). error: str if data is
    unavailable — the bot reports this honestly instead of guessing.
    """
    try:
        df = yf.Ticker(ticker).institutional_holders
    except Exception as exc:
        return [], f"Yahoo Finance returned an error: {exc}"
    if df is None or df.empty:
        return [], "No institutional holder data is available for this ticker via Yahoo Finance right now."
    rows = []
    for _, row in df.head(10).iterrows():
        try:
            rows.append((
                str(row.get("Holder", "n/a")),
                row.get("Shares"),
                row.get("% Out"),
            ))
        except Exception:
            continue
    if not rows:
        return [], "No institutional holder data is available for this ticker via Yahoo Finance right now."
    return rows, ""


def fetch_insider_alerts(ticker: str):
    """Recent insider buys from Yahoo. Returns (buys, error).

    buys: list of dicts. If Yahoo has no insider data, error explains
    that — the bot never invents insider trades.
    """
    try:
        df = yf.Ticker(ticker).insider_transactions
    except Exception as exc:
        return [], f"Yahoo Finance returned an error: {exc}"
    if df is None or df.empty:
        return [], "No insider transaction data is available for this ticker via Yahoo Finance right now."
    # Columns vary by version; find the transaction / shares / date ones.
    cols = {str(c).lower(): c for c in df.columns}
    text_col = cols.get("text") or cols.get("transaction")
    shares_col = cols.get("shares")
    date_col = cols.get("start date") or cols.get("date")
    buys = []
    cutoff = date.today() - timedelta(days=180)
    for _, row in df.iterrows():
        text = str(row.get(text_col, "")) if text_col else ""
        if "purchase" not in text.lower() and "buy" not in text.lower():
            continue
        when = row.get(date_col) if date_col else None
        try:
            when_date = when.date() if hasattr(when, "date") else None
        except Exception:
            when_date = None
        if when_date and when_date < cutoff:
            continue
        buys.append({
            "insider": str(row.get(cols.get("insider", ""), "n/a")) if cols.get("insider") else "n/a",
            "text": text,
            "shares": row.get(shares_col) if shares_col else None,
            "date": when_date,
        })
    return buys, ""

# -------------------------------------------------------- paper portfolio ---

def load_paper():
    try:
        data = json.loads(PAPER_FILE.read_text())
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def save_paper(data):
    PAPER_FILE.write_text(json.dumps(data, indent=2))


def get_paper_user(user_id: int):
    return load_paper().get(str(user_id))


def paper_value(user_data):
    """Current value of a paper portfolio. Returns (total, cash, rows).

    rows: (ticker, shares, avg_cost, price, market_value, gain_loss)
    A ticker whose price cannot be fetched is valued at its avg cost
    and flagged — never at an invented price.
    """
    cash = float(user_data.get("cash", 0))
    holdings = user_data.get("holdings", {})
    total = cash
    rows = []
    for ticker, holding in holdings.items():
        shares = float(holding.get("shares", 0))
        avg_cost = float(holding.get("avg_cost", 0))
        stock = fetch_stock(ticker)
        price = stock["price"] if stock else None
        if price is None:
            price = avg_cost  # price unavailable: value at cost, honestly
        market_value = shares * float(price)
        total += market_value
        rows.append((ticker, shares, avg_cost, price, market_value,
                     market_value - shares * avg_cost))
    return total, cash, rows


# --------------------------------------------------------------- embeds -----

def research_embed(stock: dict):
    """The full /research breakdown — same fields as the video example,
    in our own wording and layout."""
    sector = stock["sector"] or "n/a"
    if stock["industry"]:
        sector += f" — {stock['industry']}"
    change = stock["change_pct"]
    change_text = f"({change:+.1f}% today)" if change is not None else ""
    colour = 0x2ECC71 if (change or 0) >= 0 else 0xE74C3C

    embed = discord.Embed(
        title=f"{stock['name']} ({stock['ticker']})",
        description=stock["summary"] or "No company description available from Yahoo Finance.",
        colour=colour,
    )
    embed.add_field(name="Price", value=f"{fmt_money(stock['price'])} {change_text}", inline=True)
    embed.add_field(name="Market Cap", value=fmt_large(stock["market_cap"]), inline=True)
    embed.add_field(name="Sector", value=sector, inline=False)
    embed.add_field(name="P/E", value=f"{fmt_ratio(stock['pe'])} ({fmt_ratio(stock['forward_pe'])} fwd)", inline=True)
    embed.add_field(name="P/B", value=fmt_ratio(stock["pb"]), inline=True)
    embed.add_field(name="Dividend Yield", value=fmt_yield(stock["dividend_yield"]), inline=True)
    embed.add_field(
        name="52-Week Range",
        value=f"{fmt_money(stock['low_52w'])} – {fmt_money(stock['high_52w'])}",
        inline=True,
    )
    embed.add_field(name="EPS", value=fmt_number(stock["eps"]), inline=True)
    embed.add_field(name="Revenue Growth (YoY)", value=fmt_pct(stock["revenue_growth"]), inline=True)
    embed.add_field(name="Free Cash Flow", value=fmt_large(stock["free_cash_flow"]), inline=True)
    embed.add_field(name="Analyst Price Target", value=fmt_money(stock["target"]), inline=True)
    embed.add_field(name="Next Earnings Date", value=fmt_date(stock["next_earnings"]), inline=True)
    embed.set_footer(text=DISCLAIMER + " Data: Yahoo Finance.")
    return embed


def price_embed(stock: dict):
    change = stock["change_pct"]
    change_text = f"({change:+.2f}% today)" if change is not None else ""
    colour = 0x2ECC71 if (change or 0) >= 0 else 0xE74C3C
    embed = discord.Embed(
        title=f"{stock['name']} ({stock['ticker']})",
        description=f"**{fmt_money(stock['price'])}** {change_text}",
        colour=colour,
    )
    embed.add_field(name="Previous Close", value=fmt_money(stock["prev_close"]), inline=True)
    embed.add_field(
        name="Day Range",
        value=f"{fmt_money(stock['day_low'])} – {fmt_money(stock['day_high'])}",
        inline=True,
    )
    embed.add_field(
        name="52-Week Range",
        value=f"{fmt_money(stock['low_52w'])} – {fmt_money(stock['high_52w'])}",
        inline=True,
    )
    if stock["volume"] is not None:
        embed.add_field(name="Volume", value=f"{stock['volume']:,}", inline=True)
    embed.set_footer(text=DISCLAIMER + " Data: Yahoo Finance.")
    return embed


# ------------------------------------------------------------------ bot -----

class StockBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        if NEWS_CHANNEL_ID:
            self.daily_briefing.start()

    @tasks.loop(time=[dtime(9, 0, tzinfo=LONDON),
                      dtime(14, 30, tzinfo=LONDON)])
    async def daily_briefing(self):
        """09:00 briefing + 14:30 US-open update, Mon-Fri only."""
        now = datetime.now(LONDON)
        if now.weekday() >= 5:  # Saturday = 5, Sunday = 6
            return
        channel = self.get_channel(int(NEWS_CHANNEL_ID))
        if channel is None:
            return
        is_morning = now.hour < 12
        title = "☀️ Daily Market Briefing" if is_morning else "🇺🇸 US Market Open"
        lines = []
        for ticker in DEFAULT_BRIEFING_TICKERS:
            stock = await asyncio.to_thread(fetch_stock, ticker)
            if not stock:
                continue
            change = stock["change_pct"]
            change_text = f"{change:+.2f}%" if change is not None else "n/a"
            lines.append(f"**{ticker}**: {fmt_money(stock['price'])} ({change_text})")
        if not lines:
            return
        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            colour=0x3498DB,
        )
        embed.add_field(
            name="What to watch today",
            value="Check /earnings for upcoming results, and /post_week_ahead for the week ahead.",
            inline=False,
        )
        embed.set_footer(text=DISCLAIMER + " Data: Yahoo Finance.")
        try:
            await channel.send(embed=embed)
        except Exception:
            pass  # Channel deleted / no permission: don't crash the bot.

    @daily_briefing.before_loop
    async def _wait_for_briefing(self):
        await self.wait_until_ready()

    # Note: the loop above runs at BOTH 09:00 and 14:30 London time,
    # so the 09:00 briefing and the 14:30 US-open update share one task.


bot = StockBot()
tree = bot.tree


async def _fetch_or_reply(interaction, raw_ticker):
    """Fetch a stock, or reply that the ticker is invalid. Returns dict/None."""
    ticker = clean_ticker(raw_ticker)
    if not ticker:
        await interaction.followup.send(
            f"⚠️ That doesn't look like a valid ticker. Try e.g. `/research AAPL`.\n{DISCLAIMER}"
        )
        return None
    stock = await asyncio.to_thread(fetch_stock, ticker)
    if stock is None:
        await interaction.followup.send(
            f"⚠️ Couldn't find ticker **{ticker}**. Check the symbol and try again.\n{DISCLAIMER}"
        )
        return None
    return stock


# ------------------------------------------------------------- commands -----

@tree.command(name="research", description="Full research breakdown for any stock")
@app_commands.describe(ticker="Stock ticker, e.g. META, AAPL, NVDA")
async def research(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer()
    stock = await _fetch_or_reply(interaction, ticker)
    if stock:
        await interaction.followup.send(embed=research_embed(stock))


@tree.command(name="price", description="Current price of any stock")
@app_commands.describe(ticker="Stock ticker, e.g. META, AAPL")
async def price(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer()
    stock = await _fetch_or_reply(interaction, ticker)
    if stock:
        await interaction.followup.send(embed=price_embed(stock))


@tree.command(name="valuation", description="Valuation metrics for any stock")
@app_commands.describe(ticker="Stock ticker, e.g. META, AAPL")
async def valuation(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer()
    stock = await _fetch_or_reply(interaction, ticker)
    if not stock:
        return
    embed = discord.Embed(
        title=f"Valuation — {stock['name']} ({stock['ticker']})",
        description=f"Price: **{fmt_money(stock['price'])}**  •  Market cap: {fmt_large(stock['market_cap'])}",
        colour=0x3498DB,
    )
    embed.add_field(name="P/E (trailing)", value=fmt_ratio(stock["pe"]), inline=True)
    embed.add_field(name="P/E (forward)", value=fmt_ratio(stock["forward_pe"]), inline=True)
    embed.add_field(name="PEG ratio", value=fmt_ratio(stock["peg"]), inline=True)
    embed.add_field(name="P/B", value=fmt_ratio(stock["pb"]), inline=True)
    embed.add_field(name="Price / Sales", value=fmt_ratio(stock["ps"]), inline=True)
    embed.add_field(name="EV / EBITDA", value=fmt_ratio(stock["ev_ebitda"]), inline=True)
    embed.add_field(name="Dividend Yield", value=fmt_yield(stock["dividend_yield"]), inline=True)
    embed.add_field(name="Analyst Price Target", value=fmt_money(stock["target"]), inline=True)
    embed.set_footer(text=DISCLAIMER + " Data: Yahoo Finance.")
    await interaction.followup.send(embed=embed)


@tree.command(name="news", description="Latest news headlines for any stock")
@app_commands.describe(ticker="Stock ticker, e.g. META, AAPL")
async def news(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer()
    clean = clean_ticker(ticker)
    if not clean:
        await interaction.followup.send(f"⚠️ That doesn't look like a valid ticker.\n{DISCLAIMER}")
        return
    items = await asyncio.to_thread(fetch_news, clean, 5)
    if not items:
        await interaction.followup.send(
            f"No recent news found for **{clean}** via Yahoo Finance.\n{DISCLAIMER}"
        )
        return
    lines = []
    for title, url in items:
        lines.append(f"• {title}" + (f"\n  {url}" if url else ""))
    embed = discord.Embed(
        title=f"Latest news — {clean}",
        description="\n".join(lines),
        colour=0x3498DB,
    )
    embed.set_footer(text=DISCLAIMER + " Data: Yahoo Finance.")
    await interaction.followup.send(embed=embed)


@tree.command(name="earnings_result", description="Most recent earnings report for a stock")
@app_commands.describe(ticker="Stock ticker, e.g. META, AAPL")
async def earnings_result(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer()
    clean = clean_ticker(ticker)
    if not clean:
        await interaction.followup.send(f"⚠️ That doesn't look like a valid ticker.\n{DISCLAIMER}")
        return
    result = await asyncio.to_thread(fetch_latest_earnings, clean)
    if result is None:
        await interaction.followup.send(
            f"⚠️ No recent earnings data found for **{clean}** via Yahoo Finance.\n{DISCLAIMER}"
        )
        return
    embed = discord.Embed(
        title=f"Latest earnings — {clean}",
        description=f"Report date: **{fmt_date(result['date'])}**",
        colour=0x3498DB,
    )
    embed.add_field(name="Reported EPS", value=fmt_money(result.get("reported_eps")), inline=True)
    embed.add_field(name="Estimated EPS", value=fmt_money(result.get("estimated_eps")), inline=True)
    surprise = result.get("surprise_pct")
    embed.add_field(
        name="Surprise",
        value=f"{float(surprise):+.2f}%" if surprise is not None else "n/a",
        inline=True,
    )
    if result.get("revenue") is not None:
        embed.add_field(name="Revenue", value=fmt_large(result["revenue"]), inline=True)
    if result.get("net_income") is not None:
        embed.add_field(name="Net Income", value=fmt_large(result["net_income"]), inline=True)
    embed.set_footer(text=DISCLAIMER + " Data: Yahoo Finance.")
    await interaction.followup.send(embed=embed)


@tree.command(name="earnings", description="Next earnings dates — give tickers, or uses watchlist.json")
@app_commands.describe(tickers="Optional tickers separated by spaces, e.g. AAPL MSFT NVDA")
async def earnings(interaction: discord.Interaction, tickers: str = ""):
    await interaction.response.defer()
    wanted = [clean_ticker(t) for t in tickers.split()] if tickers.strip() else load_watchlist()
    wanted = [t for t in wanted if t]
    if not wanted:
        await interaction.followup.send(
            "Give me some tickers, e.g. `/earnings AAPL MSFT`, or add tickers to watchlist.json.\n"
            + DISCLAIMER
        )
        return
    results = await asyncio.to_thread(fetch_earnings_dates, wanted)
    lines = []
    for ticker in wanted:
        lines.append(f"**{ticker}**: {fmt_date(results.get(ticker))}")
    embed = discord.Embed(
        title="📅 Next Earnings Dates",
        description="\n".join(lines),
        colour=0x3498DB,
    )
    embed.set_footer(text=DISCLAIMER + " Data: Yahoo Finance.")
    await interaction.followup.send(embed=embed)


@tree.command(name="post_week_ahead", description="All upcoming earnings in the next 7 days")
async def post_week_ahead(interaction: discord.Interaction):
    await interaction.response.defer()
    wanted = load_watchlist()
    results = await asyncio.to_thread(fetch_earnings_dates, wanted)
    today = date.today()
    week = today + timedelta(days=7)
    upcoming = []
    for ticker, when in results.items():
        if when is None:
            continue
        when_date = when.date() if hasattr(when, "date") else when
        if today <= when_date <= week:
            upcoming.append((when_date, ticker))
    upcoming.sort()
    if not upcoming:
        await interaction.followup.send(
            "No earnings scheduled in the next 7 days for the tickers in watchlist.json.\n"
            "Edit watchlist.json to add more tickers.\n" + DISCLAIMER
        )
        return
    lines = [f"**{when.strftime('%a %b %d')}** — {ticker}" for when, ticker in upcoming]
    embed = discord.Embed(
        title="📅 Earnings — Week Ahead",
        description="\n".join(lines),
        colour=0x3498DB,
    )
    embed.set_footer(text=DISCLAIMER + " Data: Yahoo Finance. Edit watchlist.json to change the tickers.")
    await interaction.followup.send(embed=embed)


# --------------------------------------------------- paper (simulated) -----

@tree.command(name="paper_start", description="Start a simulated paper portfolio (fake money only)")
@app_commands.describe(starting_cash="Starting fake cash in dollars, default 10000")
async def paper_start(interaction: discord.Interaction, starting_cash: float = 10000.0):
    user_id = str(interaction.user.id)
    data = load_paper()
    if user_id in data:
        await interaction.response.send_message(
            "You already have a paper portfolio. Use /paper_portfolio to see it, "
            "or /paper_reset to start over.\n" + PAPER_DISCLAIMER,
            ephemeral=True,
        )
        return
    if starting_cash <= 0 or starting_cash > 1_000_000:
        await interaction.response.send_message(
            "Starting cash must be between $1 and $1,000,000.", ephemeral=True
        )
        return
    data[user_id] = {"cash": starting_cash, "holdings": {}, "started": date.today().isoformat()}
    save_paper(data)
    await interaction.response.send_message(
        f"✅ Paper portfolio started with **${starting_cash:,.2f}** in fake cash.\n"
        f"Try `/paper_buy AAPL 10` then `/paper_portfolio`.\n{PAPER_DISCLAIMER}",
        ephemeral=True,
    )


@tree.command(name="paper_buy", description="Buy shares in your simulated paper portfolio")
@app_commands.describe(ticker="Stock ticker, e.g. AAPL", shares="Number of shares to buy")
async def paper_buy(interaction: discord.Interaction, ticker: str, shares: float):
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    data = load_paper()
    user = data.get(user_id)
    if not user:
        await interaction.followup.send("Start first with `/paper_start`.", ephemeral=True)
        return
    clean = clean_ticker(ticker)
    if not clean or shares <= 0:
        await interaction.followup.send("Give a valid ticker and a number of shares over 0.", ephemeral=True)
        return
    stock = await asyncio.to_thread(fetch_stock, clean)
    if stock is None:
        await interaction.followup.send(f"⚠️ Couldn't find ticker **{clean}**.", ephemeral=True)
        return
    cost = float(stock["price"]) * shares
    if cost > float(user["cash"]):
        await interaction.followup.send(
            f"Not enough fake cash — that costs **${cost:,.2f}** and you have **${float(user['cash']):,.2f}**.",
            ephemeral=True,
        )
        return
    holding = user["holdings"].get(clean, {"shares": 0, "avg_cost": 0})
    old_shares = float(holding["shares"])
    old_cost = float(holding["avg_cost"])
    new_shares = old_shares + shares
    holding["avg_cost"] = (old_shares * old_cost + cost) / new_shares
    holding["shares"] = new_shares
    user["holdings"][clean] = holding
    user["cash"] = float(user["cash"]) - cost
    save_paper(data)
    await interaction.followup.send(
        f"✅ Bought **{shares:g} × {clean}** at {fmt_money(stock['price'])} "
        f"(total ${cost:,.2f}) — simulated.\n{PAPER_DISCLAIMER}",
        ephemeral=True,
    )


@tree.command(name="paper_sell", description="Sell shares from your simulated paper portfolio")
@app_commands.describe(ticker="Stock ticker, e.g. AAPL", shares="Number of shares to sell")
async def paper_sell(interaction: discord.Interaction, ticker: str, shares: float):
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    data = load_paper()
    user = data.get(user_id)
    if not user:
        await interaction.followup.send("Start first with `/paper_start`.", ephemeral=True)
        return
    clean = clean_ticker(ticker)
    holding = user["holdings"].get(clean) if clean else None
    if not holding or float(holding["shares"]) < shares or shares <= 0:
        await interaction.followup.send(f"You don't hold {shares:g} × {clean} in your paper portfolio.", ephemeral=True)
        return
    stock = await asyncio.to_thread(fetch_stock, clean)
    if stock is None:
        await interaction.followup.send(f"⚠️ Couldn't fetch a price for **{clean}** right now.", ephemeral=True)
        return
    proceeds = float(stock["price"]) * shares
    holding["shares"] = float(holding["shares"]) - shares
    if holding["shares"] == 0:
        del user["holdings"][clean]
    user["cash"] = float(user["cash"]) + proceeds
    save_paper(data)
    await interaction.followup.send(
        f"✅ Sold **{shares:g} × {clean}** at {fmt_money(stock['price'])} "
        f"(total ${proceeds:,.2f}) — simulated.\n{PAPER_DISCLAIMER}",
        ephemeral=True,
    )


@tree.command(name="paper_portfolio", description="Show your simulated paper portfolio")
async def paper_portfolio(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user = get_paper_user(interaction.user.id)
    if not user:
        await interaction.followup.send("You don't have a paper portfolio yet. Start with `/paper_start`.", ephemeral=True)
        return
    total, cash, rows = await asyncio.to_thread(paper_value, user)
    embed = discord.Embed(
        title=f"📄 {interaction.user.display_name}'s Paper Portfolio",
        description=f"Total value: **${total:,.2f}**  •  Cash: **${cash:,.2f}**",
        colour=0x3498DB,
    )
    if not rows:
        embed.add_field(name="Holdings", value="No holdings yet — try `/paper_buy AAPL 10`.", inline=False)
    for ticker, shares, avg_cost, price, market_value, gain in rows:
        sign = "+" if gain >= 0 else "−"
        embed.add_field(
            name=ticker,
            value=f"{shares:g} shares @ {fmt_money(avg_cost)} avg\n"
                  f"Now {fmt_money(price)} • Value ${market_value:,.2f} • {sign}${abs(gain):,.2f}",
            inline=True,
        )
    embed.set_footer(text=PAPER_DISCLAIMER + " Prices: Yahoo Finance.")
    await interaction.followup.send(embed=embed)


@tree.command(name="paper_reset", description="Delete your simulated paper portfolio and start over")
async def paper_reset(interaction: discord.Interaction):
    data = load_paper()
    if str(interaction.user.id) in data:
        del data[str(interaction.user.id)]
        save_paper(data)
    await interaction.response.send_message("Your paper portfolio has been reset. Use /paper_start to begin again.", ephemeral=True)


# ------------------------------------------- institutional / insider -------

@tree.command(name="institutional_list", description="Top institutional holders for a stock (Yahoo data)")
@app_commands.describe(ticker="Stock ticker, e.g. META, AAPL")
async def institutional_list(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer()
    clean = clean_ticker(ticker)
    if not clean:
        await interaction.followup.send(f"⚠️ That doesn't look like a valid ticker.\n{DISCLAIMER}")
        return
    rows, error = await asyncio.to_thread(fetch_institutional, clean)
    if error:
        await interaction.followup.send(
            f"⚠️ **{clean}** — {error}\n"
            "Note: quarterly 13F filings are not available through free Yahoo Finance data, "
            "so this bot cannot auto-post new 13F filings.\n" + DISCLAIMER
        )
        return
    lines = []
    for holder, shares, pct in rows:
        shares_text = f"{float(shares):,.0f} shares" if shares is not None else "n/a"
        pct_text = f"{float(pct):.2f}%" if pct is not None else "n/a"
        lines.append(f"• **{holder}** — {shares_text} ({pct_text} of shares)")
    embed = discord.Embed(
        title=f"🏦 Institutional holders — {clean}",
        description="\n".join(lines),
        colour=0x3498DB,
    )
    embed.set_footer(text=DISCLAIMER + " Data: Yahoo Finance. 13F auto-posting is not available with free data.")
    await interaction.followup.send(embed=embed)


@tree.command(name="check_insider_alerts", description="Recent insider buys for a stock (Yahoo data)")
@app_commands.describe(ticker="Stock ticker, e.g. META, AAPL")
async def check_insider_alerts(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer()
    clean = clean_ticker(ticker)
    if not clean:
        await interaction.followup.send(f"⚠️ That doesn't look like a valid ticker.\n{DISCLAIMER}")
        return
    buys, error = await asyncio.to_thread(fetch_insider_alerts, clean)
    if error:
        await interaction.followup.send(f"⚠️ **{clean}** — {error}\n{DISCLAIMER}")
        return
    if not buys:
        await interaction.followup.send(
            f"No insider buys found for **{clean}** in the last 180 days in Yahoo Finance data. "
            "That means no buys were reported — not that none happened.\n" + DISCLAIMER
        )
        return
    lines = []
    for buy in buys[:10]:
        lines.append(
            f"• {buy['insider']} — {buy['text']}"
            + (f" ({buy['shares']:,.0f} shares)" if buy["shares"] else "")
            + (f" — {fmt_date(buy['date'])}" if buy["date"] else "")
        )
    embed = discord.Embed(
        title=f"🔍 Insider buys — {clean} (last 180 days)",
        description="\n".join(lines),
        colour=0x3498DB,
    )
    embed.set_footer(text=DISCLAIMER + " Data: Yahoo Finance.")
    await interaction.followup.send(embed=embed)


@tree.command(name="help", description="List all Stock Research Bot commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📈 Stock Research Bot — Commands",
        description="Research any stock. Works for any ticker — this bot is not linked to anyone's personal portfolio.",
        colour=0x3498DB,
    )
    embed.add_field(name="Stock Research", value=(
        "/research [TICKER] — Full research breakdown\n"
        "/price [TICKER] — Current price\n"
        "/valuation [TICKER] — Valuation metrics\n"
        "/news [TICKER] — Latest headlines"
    ), inline=False)
    embed.add_field(name="Earnings", value=(
        "/earnings_result [TICKER] — Most recent earnings report\n"
        "/earnings [tickers] — Next earnings dates\n"
        "/post_week_ahead — Earnings in the next 7 days"
    ), inline=False)
    embed.add_field(name="Paper Trading (simulated)", value=(
        "/paper_start — Start with $10,000 fake cash\n"
        "/paper_buy / /paper_sell — Trade with fake money\n"
        "/paper_portfolio — See your paper holdings\n"
        "/paper_reset — Start over"
    ), inline=False)
    embed.add_field(name="Institutional & Insider", value=(
        "/institutional_list [TICKER] — Top institutional holders\n"
        "/check_insider_alerts [TICKER] — Recent insider buys"
    ), inline=False)
    embed.set_footer(text=DISCLAIMER)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Slash commands synced. News channel: {NEWS_CHANNEL_ID or 'not set — scheduled posts disabled'}")


# ------------------------------------------------------------------ test -----

def print_research(stock: dict):
    """Plain-text version of /research, for --test mode (no Discord)."""
    print(f"\n{stock['name']} ({stock['ticker']})")
    if stock["summary"]:
        print(stock["summary"][:500])
    change = stock["change_pct"]
    print(f"Price: {fmt_money(stock['price'])}" + (f" ({change:+.2f}% today)" if change is not None else ""))
    print(f"Market Cap: {fmt_large(stock['market_cap'])}")
    print(f"Sector: {stock['sector'] or 'n/a'}" + (f" — {stock['industry']}" if stock["industry"] else ""))
    print(f"P/E: {fmt_ratio(stock['pe'])} ({fmt_ratio(stock['forward_pe'])} fwd)")
    print(f"P/B: {fmt_ratio(stock['pb'])}")
    print(f"Dividend Yield: {fmt_yield(stock["dividend_yield"])}")
    print(f"52-Week Range: {fmt_money(stock['low_52w'])} – {fmt_money(stock['high_52w'])}")
    print(f"EPS: {fmt_number(stock['eps'])}")
    print(f"Revenue Growth (YoY): {fmt_pct(stock['revenue_growth'])}")
    print(f"Free Cash Flow: {fmt_large(stock['free_cash_flow'])}")
    print(f"Analyst Price Target: {fmt_money(stock['target'])}")
    print(f"Next Earnings Date: {fmt_date(stock['next_earnings'])}")


def run_test(tickers):
    """Test the data functions without logging into Discord."""
    for ticker in tickers:
        print(f"\n===== /research {ticker} =====")
        stock = fetch_stock(ticker)
        if stock is None:
            print(f"FAILED: no price data for {ticker}")
            continue
        print_research(stock)
        news_items = fetch_news(ticker, 3)
        print(f"News: {len(news_items)} headline(s)")
        for title, url in news_items:
            print(f"  - {title}")
        print(f"Next earnings: {fmt_date(stock['next_earnings'])}")


def main():
    if "--test" in sys.argv:
        tickers = [a for a in sys.argv[1:] if not a.startswith("-")]
        run_test(tickers or ["META"])
        return
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN is not set.")
        print("Copy .env.example to .env and add your bot token, then run: python bot.py")
        print("To test the data functions without a token, run: python bot.py --test META")
        sys.exit(1)
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
