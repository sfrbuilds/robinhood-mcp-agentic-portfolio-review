"""
ticker_analysis.py
==================
Comprehensive technical + fundamental analysis for any US equity.

Designed to be called by a Claude Code agent alongside the Robinhood
Trading MCP — the MCP provides live account/position data, this module
provides the analytical layer the official MCP doesn't expose.

Usage:
    # Single ticker
    python ticker_analysis.py AAPL

    # Multiple tickers
    python ticker_analysis.py AAPL NVDA MSFT

    # Import in agent scripts
    from research.ticker_analysis import analyze, analyze_many

Output: JSON to stdout (or returned as dict when imported).

Install:
    pip install yfinance pandas numpy
"""

import sys
import json
import math
import warnings
from datetime import datetime, timezone, date
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe(val, fallback=None):
    """Return val unless it is NaN, inf, or None."""
    try:
        if val is None:
            return fallback
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return fallback
        return round(f, 4)
    except Exception:
        return fallback


def _pct(new, old, decimals=2):
    """Percent change from old to new."""
    if old is None or old == 0:
        return None
    return round((new - old) / abs(old) * 100, decimals)


def _last(series):
    """Last non-NaN value of a pandas Series."""
    s = series.dropna()
    return float(s.iloc[-1]) if len(s) else None


def _ago(df, n_days):
    """Row n_days back from the end of a daily DataFrame."""
    if len(df) <= n_days:
        return None
    return df.iloc[-(n_days + 1)]


# ── Core analysis ─────────────────────────────────────────────────────────────

def analyze(symbol: str, period: str = "2y") -> dict:
    """
    Return a comprehensive analysis dict for `symbol`.

    Sections
    --------
    meta        : symbol, name, sector, industry, exchange
    price       : current, prev_close, day_change
    performance : 7d / 30d / 90d / YTD / 1y / 52w
    technicals  : MACD, RSI, EMAs, Bollinger Bands, ATR, volume
    fundamentals: P/E, P/S, margins, growth, dividend, next earnings
    analyst     : consensus, price target, buy/hold/sell breakdown
    signal      : quick summary for agent consumption
    """

    result = {"symbol": symbol.upper(), "as_of": date.today().isoformat(), "error": None}

    try:
        ticker = yf.Ticker(symbol)
        info   = ticker.info or {}

        # ── Price history ─────────────────────────────────────────────────────
        hist = ticker.history(period=period, auto_adjust=True)
        if hist is None or len(hist) < 10:
            result["error"] = "Insufficient price history"
            return result

        hist.index = pd.to_datetime(hist.index).tz_localize(None)
        close  = hist["Close"]
        high   = hist["High"]
        low    = hist["Low"]
        volume = hist["Volume"]

        price_now  = _last(close)
        prev_close = float(close.iloc[-2]) if len(close) >= 2 else price_now

        # ── Meta ──────────────────────────────────────────────────────────────
        result["meta"] = {
            "name":     info.get("longName") or info.get("shortName", symbol),
            "sector":   info.get("sector", "Unknown"),
            "industry": info.get("industry", "Unknown"),
            "exchange": info.get("exchange", "Unknown"),
        }

        # ── Current price ─────────────────────────────────────────────────────
        result["price"] = {
            "current":        _safe(price_now),
            "prev_close":     _safe(prev_close),
            "day_change_pct": _safe(_pct(price_now, prev_close)),
            "bid":            _safe(info.get("bid")),
            "ask":            _safe(info.get("ask")),
        }

        # ── Performance ───────────────────────────────────────────────────────
        # Helper: price N trading days back
        def price_n_back(n):
            if len(close) > n:
                return float(close.iloc[-(n + 1)])
            return None

        # YTD: first trading day of the year
        this_year = date.today().year
        ytd_mask  = hist.index.year == this_year
        ytd_open  = float(close[ytd_mask].iloc[0]) if ytd_mask.any() else None

        # 52-week window
        w52_close = close.iloc[-252:] if len(close) >= 252 else close
        w52_high  = float(w52_close.max())
        w52_low   = float(w52_close.min())
        price_1y_back = price_n_back(252) or price_n_back(len(close) - 1)

        # 7-day and 30-day windows
        h7  = high.iloc[-7:]
        l7  = low.iloc[-7:]
        h30 = high.iloc[-30:]
        l30 = low.iloc[-30:]
        h90 = high.iloc[-90:]
        l90 = low.iloc[-90:]

        result["performance"] = {
            "7d": {
                "return_pct": _safe(_pct(price_now, price_n_back(7))),
                "high":       _safe(float(h7.max())),
                "low":        _safe(float(l7.min())),
            },
            "30d": {
                "return_pct": _safe(_pct(price_now, price_n_back(30))),
                "high":       _safe(float(h30.max())),
                "low":        _safe(float(l30.min())),
            },
            "90d": {
                "return_pct": _safe(_pct(price_now, price_n_back(90))),
                "high":       _safe(float(h90.max())),
                "low":        _safe(float(l90.min())),
            },
            "ytd": {
                "return_pct": _safe(_pct(price_now, ytd_open)),
                "start_price": _safe(ytd_open),
            },
            "1y": {
                "return_pct": _safe(_pct(price_now, price_1y_back)),
            },
            "52w": {
                "high":             _safe(w52_high),
                "low":              _safe(w52_low),
                "pct_below_high":   _safe(_pct(price_now, w52_high)),
                "pct_above_low":    _safe(_pct(price_now, w52_low)),
            },
        }

        # ── Technicals ────────────────────────────────────────────────────────

        # MACD (12 / 26 / 9)
        exp12 = close.ewm(span=12, adjust=False).mean()
        exp26 = close.ewm(span=26, adjust=False).mean()
        macd_line   = exp12 - exp26
        macd_signal = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist   = macd_line - macd_signal

        ml_now  = _last(macd_line)
        ms_now  = _last(macd_signal)
        mh_now  = _last(macd_hist)
        ml_prev = float(macd_line.iloc[-2]) if len(macd_line) >= 2 else None
        ms_prev = float(macd_signal.iloc[-2]) if len(macd_signal) >= 2 else None

        # Crossover detection (up to 5 days back)
        crossover     = "none"
        days_since    = None
        for i in range(1, 6):
            if len(macd_line) > i + 1:
                cur_above  = macd_line.iloc[-i]    > macd_signal.iloc[-i]
                prev_above = macd_line.iloc[-i - 1] > macd_signal.iloc[-i - 1]
                if cur_above and not prev_above:
                    crossover  = "bullish"
                    days_since = i
                    break
                elif not cur_above and prev_above:
                    crossover  = "bearish"
                    days_since = i
                    break

        # RSI (14)
        delta = close.diff()
        gain  = delta.where(delta > 0, 0.0).rolling(14).mean()
        loss  = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi   = 100 - 100 / (1 + rs)
        rsi_now = _last(rsi)

        # EMAs
        ema20  = close.ewm(span=20,  adjust=False).mean()
        ema50  = close.ewm(span=50,  adjust=False).mean()
        ema200 = close.ewm(span=200, adjust=False).mean()
        ema20_val  = _last(ema20)
        ema50_val  = _last(ema50)
        ema200_val = _last(ema200)

        # ATR (14)
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr14 = _last(tr.rolling(14).mean())
        atr_pct = round((atr14 / price_now) * 100, 3) if atr14 and price_now else None

        # Bollinger Bands (20, 2σ)
        bb_mid   = close.rolling(20).mean()
        bb_std   = close.rolling(20).std()
        bb_upper = bb_mid + 2 * bb_std
        bb_lower = bb_mid - 2 * bb_std
        bbu = _last(bb_upper)
        bbm = _last(bb_mid)
        bbl = _last(bb_lower)
        bb_pct = round((price_now - bbl) / (bbu - bbl), 4) if bbu and bbl and bbu != bbl else None

        # Volume
        vol_now    = float(volume.iloc[-1]) if len(volume) else None
        vol_20avg  = _last(volume.rolling(20).mean())
        rel_vol    = round(vol_now / vol_20avg, 2) if vol_now and vol_20avg and vol_20avg > 0 else None

        result["technicals"] = {
            "macd": {
                "line":         _safe(ml_now),
                "signal":       _safe(ms_now),
                "histogram":    _safe(mh_now),
                "crossover":    crossover,       # "bullish" | "bearish" | "none"
                "days_since_cross": days_since,  # None if no recent cross
                "trend":        "bullish" if ml_now and ms_now and ml_now > ms_now else "bearish",
            },
            "rsi_14":         _safe(rsi_now),
            "rsi_zone":       (
                "overbought" if rsi_now and rsi_now > 70 else
                "oversold"   if rsi_now and rsi_now < 30 else
                "neutral"
            ),
            "ema_20":         _safe(ema20_val),
            "ema_50":         _safe(ema50_val),
            "ema_200":        _safe(ema200_val),
            "above_ema_20":   bool(price_now > ema20_val)  if ema20_val  else None,
            "above_ema_50":   bool(price_now > ema50_val)  if ema50_val  else None,
            "above_ema_200":  bool(price_now > ema200_val) if ema200_val else None,
            "golden_cross":   bool(ema50_val > ema200_val) if ema50_val and ema200_val else None,
            "atr_14":         _safe(atr14),
            "atr_pct":        _safe(atr_pct),
            "bollinger": {
                "upper":   _safe(bbu),
                "middle":  _safe(bbm),
                "lower":   _safe(bbl),
                "pct_b":   _safe(bb_pct),   # 0=at lower band, 1=at upper band
                "squeeze": bool((bbu - bbl) / bbm < 0.05) if bbu and bbl and bbm else None,
            },
            "volume": {
                "today":    int(vol_now)    if vol_now   else None,
                "avg_20d":  int(vol_20avg)  if vol_20avg else None,
                "rel_vol":  _safe(rel_vol),
            },
        }

        # ── Fundamentals ──────────────────────────────────────────────────────
        def _billions(v):
            v = info.get(v)
            if v is None:
                return None
            return round(v / 1e9, 2)

        # Next earnings date
        try:
            cal = ticker.calendar
            if cal is not None and hasattr(cal, 'get'):
                earnings_raw = cal.get("Earnings Date")
                if earnings_raw is not None:
                    if hasattr(earnings_raw, '__iter__') and not isinstance(earnings_raw, str):
                        earnings_raw = list(earnings_raw)
                        next_earnings = str(earnings_raw[0])[:10] if earnings_raw else None
                    else:
                        next_earnings = str(earnings_raw)[:10]
                else:
                    next_earnings = None
            elif cal is not None and isinstance(cal, pd.DataFrame) and not cal.empty:
                try:
                    next_earnings = str(cal.loc["Earnings Date"].iloc[0])[:10]
                except Exception:
                    next_earnings = None
            else:
                next_earnings = None
        except Exception:
            next_earnings = None

        result["fundamentals"] = {
            "market_cap_b":         _billions("marketCap"),
            "pe_ttm":               _safe(info.get("trailingPE")),
            "pe_forward":           _safe(info.get("forwardPE")),
            "ps_ratio":             _safe(info.get("priceToSalesTrailing12Months")),
            "pb_ratio":             _safe(info.get("priceToBook")),
            "peg_ratio":            _safe(info.get("pegRatio")),
            "ev_ebitda":            _safe(info.get("enterpriseToEbitda")),
            "profit_margin_pct":    _safe(
                round(info["profitMargins"] * 100, 2) if info.get("profitMargins") else None
            ),
            "operating_margin_pct": _safe(
                round(info["operatingMargins"] * 100, 2) if info.get("operatingMargins") else None
            ),
            "revenue_growth_yoy":   _safe(
                round(info["revenueGrowth"] * 100, 2) if info.get("revenueGrowth") else None
            ),
            "earnings_growth_yoy":  _safe(
                round(info["earningsGrowth"] * 100, 2) if info.get("earningsGrowth") else None
            ),
            "return_on_equity_pct": _safe(
                round(info["returnOnEquity"] * 100, 2) if info.get("returnOnEquity") else None
            ),
            "debt_to_equity":       _safe(info.get("debtToEquity")),
            "current_ratio":        _safe(info.get("currentRatio")),
            "dividend_yield_pct":   _safe(
                # Compute from annual payout rate — more reliable than dividendYield field
                round(info["trailingAnnualDividendRate"] / price_now * 100, 2)
                if info.get("trailingAnnualDividendRate") and price_now
                else None
            ),
            "short_float_pct":      _safe(
                round(info["shortPercentOfFloat"] * 100, 2) if info.get("shortPercentOfFloat") else None
            ),
            "next_earnings":        next_earnings,
            "beta":                 _safe(info.get("beta")),
        }

        # ── Analyst ───────────────────────────────────────────────────────────
        try:
            recs = ticker.recommendations
            if recs is not None and len(recs) > 0:
                # Get last 90 days of recommendations
                recent = recs.tail(20)
                buy_cols  = [c for c in recent.columns if "buy"    in c.lower() and "strong" not in c.lower()]
                sbuy_cols = [c for c in recent.columns if "strong" in c.lower() and "buy"    in c.lower()]
                hold_cols = [c for c in recent.columns if "hold"   in c.lower()]
                sell_cols = [c for c in recent.columns if "sell"   in c.lower() and "strong" not in c.lower()]
                ssell_cols= [c for c in recent.columns if "strong" in c.lower() and "sell"   in c.lower()]

                buys  = int(recent[sbuy_cols + buy_cols].sum().sum()) if sbuy_cols or buy_cols else 0
                holds = int(recent[hold_cols].sum().sum()) if hold_cols else 0
                sells = int(recent[sell_cols + ssell_cols].sum().sum()) if sell_cols or ssell_cols else 0
                total = buys + holds + sells
                consensus = "Buy" if buys > holds and buys > sells else ("Sell" if sells > buys else "Hold")
            else:
                buys = holds = sells = total = 0
                consensus = None
        except Exception:
            buys = holds = sells = total = 0
            consensus = None

        pt = info.get("targetMeanPrice")
        result["analyst"] = {
            "consensus":            consensus,
            "price_target_avg":     _safe(pt),
            "price_target_upside":  _safe(_pct(pt, price_now)) if pt else None,
            "num_analysts":         info.get("numberOfAnalystOpinions"),
            "buy_count":            buys  if total else None,
            "hold_count":           holds if total else None,
            "sell_count":           sells if total else None,
        }

        # ── Quick signal summary (for agent consumption) ───────────────────────
        tech = result["technicals"]
        perf = result["performance"]

        flags = []
        if tech["macd"]["crossover"] == "bullish":
            flags.append(f"bullish MACD cross {tech['macd']['days_since_cross']}d ago")
        if tech["macd"]["crossover"] == "bearish":
            flags.append(f"bearish MACD cross {tech['macd']['days_since_cross']}d ago")
        if tech["rsi_14"] and tech["rsi_14"] > 70:
            flags.append(f"RSI overbought ({tech['rsi_14']:.0f})")
        if tech["rsi_14"] and tech["rsi_14"] < 30:
            flags.append(f"RSI oversold ({tech['rsi_14']:.0f})")
        if tech["golden_cross"]:
            flags.append("golden cross (EMA50 > EMA200)")
        elif tech["golden_cross"] is False:
            flags.append("death cross (EMA50 < EMA200)")
        if tech["volume"]["rel_vol"] and tech["volume"]["rel_vol"] > 2.0:
            flags.append(f"volume spike {tech['volume']['rel_vol']:.1f}x avg")
        if tech["bollinger"]["pct_b"] and tech["bollinger"]["pct_b"] > 0.95:
            flags.append("near upper Bollinger band")
        if tech["bollinger"]["pct_b"] and tech["bollinger"]["pct_b"] < 0.05:
            flags.append("near lower Bollinger band")
        if tech["bollinger"]["squeeze"]:
            flags.append("Bollinger squeeze (low volatility, potential breakout pending)")
        if perf["52w"]["pct_below_high"] and perf["52w"]["pct_below_high"] > -3:
            flags.append("near 52-week high")
        if perf["52w"]["pct_below_high"] and perf["52w"]["pct_below_high"] < -30:
            flags.append("more than 30% below 52-week high")

        result["signal_flags"] = flags

    except Exception as e:
        result["error"] = str(e)

    return result


def analyze_many(symbols: list, period: str = "2y") -> list:
    """Analyze multiple tickers. Returns list of analysis dicts."""
    return [analyze(s, period) for s in symbols]


# Sector ticker -> ETF mapping
_SECTOR_ETF = {
    "Technology":              "XLK",
    "Communication Services":  "XLC",
    "Consumer Cyclical":       "XLY",
    "Consumer Defensive":      "XLP",
    "Healthcare":              "XLV",
    "Financials":              "XLF",
    "Industrials":             "XLI",
    "Basic Materials":         "XLB",
    "Real Estate":             "XLRE",
    "Utilities":               "XLU",
    "Energy":                  "XLE",
}


def market_context(extra_sectors: list = None) -> dict:
    """
    Fetch macro regime data: SPY, VIX, and optionally sector ETFs.

    Returns a dict with keys: spy, vix, sectors, regime_flags.
    Designed to be printed once before ticker analysis output.
    """
    ctx = {"as_of": date.today().isoformat(), "spy": {}, "vix": {}, "sectors": {}, "regime_flags": []}

    # ── SPY ───────────────────────────────────────────────────────────────────
    try:
        spy_hist = yf.Ticker("SPY").history(period="1y", auto_adjust=True)
        spy_hist.index = pd.to_datetime(spy_hist.index).tz_localize(None)
        sc = spy_hist["Close"]

        spy_now   = float(sc.iloc[-1])
        spy_prev  = float(sc.iloc[-2]) if len(sc) >= 2 else spy_now
        ema20_spy = float(sc.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50_spy = float(sc.ewm(span=50, adjust=False).mean().iloc[-1])
        ema200_spy= float(sc.ewm(span=200, adjust=False).mean().iloc[-1])

        # YTD
        this_year = date.today().year
        ytd_mask  = spy_hist.index.year == this_year
        ytd_open  = float(sc[ytd_mask].iloc[0]) if ytd_mask.any() else None
        ret_30d   = _pct(spy_now, float(sc.iloc[-30])) if len(sc) >= 30 else None

        ctx["spy"] = {
            "price":          _safe(spy_now),
            "day_change_pct": _safe(_pct(spy_now, spy_prev)),
            "return_30d_pct": _safe(ret_30d),
            "return_ytd_pct": _safe(_pct(spy_now, ytd_open)),
            "ema_20":         _safe(ema20_spy),
            "ema_50":         _safe(ema50_spy),
            "ema_200":        _safe(ema200_spy),
            "above_ema_20":   bool(spy_now > ema20_spy),
            "golden_cross":   bool(ema50_spy > ema200_spy),
        }

        # Regime flags
        if spy_now < ema20_spy:
            ctx["regime_flags"].append("SPY below EMA20 — engine entries BLOCKED")
        else:
            ctx["regime_flags"].append("SPY above EMA20 — regime OK")

        if not (ema50_spy > ema200_spy):
            ctx["regime_flags"].append("SPY death cross (EMA50 < EMA200)")

    except Exception as e:
        ctx["spy"]["error"] = str(e)

    # ── VIX ───────────────────────────────────────────────────────────────────
    try:
        vix_hist = yf.Ticker("^VIX").history(period="30d", auto_adjust=True)
        vix_hist.index = pd.to_datetime(vix_hist.index).tz_localize(None)
        vc = vix_hist["Close"]
        vix_now  = float(vc.iloc[-1])
        vix_prev = float(vc.iloc[-2]) if len(vc) >= 2 else vix_now
        vix_30d_avg = float(vc.mean())

        ctx["vix"] = {
            "current":       _safe(vix_now),
            "prev_close":    _safe(vix_prev),
            "day_change_pct":_safe(_pct(vix_now, vix_prev)),
            "avg_30d":       _safe(vix_30d_avg),
            "hard_block":    vix_now >= 40,
        }

        if vix_now >= 40:
            ctx["regime_flags"].append(f"VIX {vix_now:.1f} >= 40 — hard block on ALL entries")
        elif vix_now >= 25:
            ctx["regime_flags"].append(f"VIX {vix_now:.1f} — elevated, size down")
        else:
            ctx["regime_flags"].append(f"VIX {vix_now:.1f} — normal")

    except Exception as e:
        ctx["vix"]["error"] = str(e)

    # ── Sector ETFs ───────────────────────────────────────────────────────────
    etfs_to_fetch = list(set(list(_SECTOR_ETF.values()) + (extra_sectors or [])))
    for etf in etfs_to_fetch:
        try:
            h = yf.Ticker(etf).history(period="3mo", auto_adjust=True)
            if h is None or len(h) < 5:
                continue
            h.index = pd.to_datetime(h.index).tz_localize(None)
            ec = h["Close"]
            ep = float(ec.iloc[-1])
            ep_prev = float(ec.iloc[-2]) if len(ec) >= 2 else ep
            ema20_e = float(ec.ewm(span=20, adjust=False).mean().iloc[-1])
            ret30 = _pct(ep, float(ec.iloc[-30])) if len(ec) >= 30 else None
            ctx["sectors"][etf] = {
                "price":          _safe(ep),
                "day_change_pct": _safe(_pct(ep, ep_prev)),
                "return_30d_pct": _safe(ret30),
                "above_ema_20":   bool(ep > ema20_e),
                "trend":          "bullish" if ep > ema20_e else "bearish",
            }
        except Exception:
            pass

    return ctx


def _fmt_context(ctx: dict) -> str:
    """Format market_context() dict as a header block for CLI output."""
    spy = ctx.get("spy", {})
    vix = ctx.get("vix", {})
    sectors = ctx.get("sectors", {})
    flags = ctx.get("regime_flags", [])

    lines = [
        f"{'#'*60}",
        f"MARKET CONTEXT  —  {ctx.get('as_of', '')}",
        f"{'#'*60}",
        "",
        f"SPY   ${spy.get('price', 'n/a')}  "
        f"day {spy.get('day_change_pct', 'n/a')}%  "
        f"30d {spy.get('return_30d_pct', 'n/a')}%  "
        f"YTD {spy.get('return_ytd_pct', 'n/a')}%",
        f"      EMA20 {spy.get('ema_20', 'n/a')}  "
        f"EMA50 {spy.get('ema_50', 'n/a')}  "
        f"EMA200 {spy.get('ema_200', 'n/a')}  "
        f"{'[above EMA20]' if spy.get('above_ema_20') else '[BELOW EMA20]'}  "
        f"{'[golden cross]' if spy.get('golden_cross') else '[death cross]'}",
        "",
        f"VIX   {vix.get('current', 'n/a')}  "
        f"(prev {vix.get('prev_close', 'n/a')}  "
        f"30d avg {vix.get('avg_30d', 'n/a')})"
        + ("  *** HARD BLOCK ***" if vix.get("hard_block") else ""),
        "",
    ]

    if sectors:
        lines.append("SECTOR ETFs (30d return / trend)")
        # Sort by 30d return descending
        sorted_etfs = sorted(
            sectors.items(),
            key=lambda x: x[1].get("return_30d_pct") or -999,
            reverse=True
        )
        for etf, d in sorted_etfs:
            trend_icon = "+" if d.get("trend") == "bullish" else "-"
            lines.append(
                f"  {etf:<5}  {str(d.get('return_30d_pct', 'n/a')):>7}%  "
                f"day {str(d.get('day_change_pct', 'n/a')):>6}%  "
                f"[{trend_icon}]"
            )
        lines.append("")

    if flags:
        lines.append("REGIME CHECKS")
        for flag in flags:
            lines.append(f"  {flag}")
        lines.append("")

    lines.append("")
    return "\n".join(lines)


def _fmt(data: dict) -> str:
    """Format analysis dict as clean human-readable text (for Claude context)."""
    if data.get("error"):
        return f"{data['symbol']}: ERROR — {data['error']}"

    m = data.get("meta", {})
    p = data.get("price", {})
    perf = data.get("performance", {})
    t = data.get("technicals", {})
    f = data.get("fundamentals", {})
    a = data.get("analyst", {})
    flags = data.get("signal_flags", [])

    lines = [
        f"{'='*60}",
        f"{data['symbol']}  —  {m.get('name', '')}",
        f"{m.get('sector', '')} / {m.get('industry', '')}",
        f"as of {data['as_of']}",
        f"{'='*60}",
        "",
        f"PRICE",
        f"  Current:    ${p.get('current', 'n/a')}",
        f"  Prev close: ${p.get('prev_close', 'n/a')}",
        f"  Day change: {p.get('day_change_pct', 'n/a')}%",
        "",
        f"PERFORMANCE",
    ]

    for label, key in [("7d", "7d"), ("30d", "30d"), ("90d", "90d"), ("YTD", "ytd"), ("1Y", "1y")]:
        d = perf.get(key, {})
        ret = d.get("return_pct", "n/a")
        h   = d.get("high")
        l   = d.get("low")
        hl  = f"  H: ${h}  L: ${l}" if h and l else ""
        lines.append(f"  {label:>4}: {str(ret):>8}%{hl}")

    w = perf.get("52w", {})
    lines.append(f"  52w:  H ${w.get('high', 'n/a')}  L ${w.get('low', 'n/a')}  "
                 f"({w.get('pct_below_high', 'n/a')}% from high)")

    macd = t.get("macd", {})
    bb   = t.get("bollinger", {})
    vol  = t.get("volume", {})

    lines += [
        "",
        f"TECHNICALS",
        f"  RSI (14):       {t.get('rsi_14', 'n/a')}  [{t.get('rsi_zone', '')}]",
        f"  MACD:           line {macd.get('line', 'n/a')}  signal {macd.get('signal', 'n/a')}  "
        f"hist {macd.get('histogram', 'n/a')}",
        f"  MACD trend:     {macd.get('trend', 'n/a')}  |  crossover: {macd.get('crossover', 'none')}  "
        + (f"({macd.get('days_since_cross')}d ago)" if macd.get('days_since_cross') else ""),
        f"  EMA 20/50/200:  {t.get('ema_20', 'n/a')} / {t.get('ema_50', 'n/a')} / {t.get('ema_200', 'n/a')}",
        f"  vs EMAs:        20 {'above' if t.get('above_ema_20') else 'below'}  "
        f"50 {'above' if t.get('above_ema_50') else 'below'}  "
        f"200 {'above' if t.get('above_ema_200') else 'below'}",
        f"  Golden cross:   {'YES' if t.get('golden_cross') else 'NO'}",
        f"  ATR (14):       ${t.get('atr_14', 'n/a')}  ({t.get('atr_pct', 'n/a')}% of price)",
        f"  Bollinger:      U ${bb.get('upper', 'n/a')}  M ${bb.get('middle', 'n/a')}  L ${bb.get('lower', 'n/a')}  "
        f"%B {bb.get('pct_b', 'n/a')}{'  [SQUEEZE]' if bb.get('squeeze') else ''}",
        f"  Volume:         {vol.get('today', 'n/a'):,}  vs 20d avg {vol.get('avg_20d', 'n/a'):,}  "
        f"({vol.get('rel_vol', 'n/a')}x)" if vol.get('today') and vol.get('avg_20d') else
        f"  Volume:         n/a",
        "",
        f"FUNDAMENTALS",
        f"  Market cap:     ${f.get('market_cap_b', 'n/a')}B",
        f"  P/E (TTM/Fwd):  {f.get('pe_ttm', 'n/a')} / {f.get('pe_forward', 'n/a')}",
        f"  P/S:            {f.get('ps_ratio', 'n/a')}",
        f"  EV/EBITDA:      {f.get('ev_ebitda', 'n/a')}",
        f"  Profit margin:  {f.get('profit_margin_pct', 'n/a')}%",
        f"  Rev growth YoY: {f.get('revenue_growth_yoy', 'n/a')}%",
        f"  EPS growth YoY: {f.get('earnings_growth_yoy', 'n/a')}%",
        f"  ROE:            {f.get('return_on_equity_pct', 'n/a')}%",
        f"  Dividend yield: {f.get('dividend_yield_pct', 'n/a')}%",
        f"  Short float:    {f.get('short_float_pct', 'n/a')}%",
        f"  Beta:           {f.get('beta', 'n/a')}",
        f"  Next earnings:  {f.get('next_earnings', 'n/a')}",
        "",
        f"ANALYST",
        f"  Consensus:      {a.get('consensus', 'n/a')}",
        f"  Avg PT:         ${a.get('price_target_avg', 'n/a')}  "
        f"({a.get('price_target_upside', 'n/a')}% upside)",
        f"  Coverage:       {a.get('num_analysts', 'n/a')} analysts  "
        f"B:{a.get('buy_count', '?')} H:{a.get('hold_count', '?')} S:{a.get('sell_count', '?')}",
    ]

    if flags:
        lines += ["", "SIGNAL FLAGS"]
        for flag in flags:
            lines.append(f"  * {flag}")

    lines.append("")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args    = sys.argv[1:]
    as_json = "--json"     in args
    no_macro= "--no-macro" in args
    symbols = [s for s in args if not s.startswith("--")] or ["SPY"]

    if not as_json:
        if not no_macro:
            # Collect sector ETFs relevant to the requested tickers
            extra_etfs = []
            for sym in symbols:
                try:
                    sector = yf.Ticker(sym).info.get("sector", "")
                    etf = _SECTOR_ETF.get(sector)
                    if etf and etf not in extra_etfs:
                        extra_etfs.append(etf)
                except Exception:
                    pass
            ctx = market_context(extra_sectors=extra_etfs)
            print(_fmt_context(ctx))
        for sym in symbols:
            print(_fmt(analyze(sym)))
    else:
        output = {"market": market_context() if not no_macro else None}
        output["tickers"] = [analyze(s) for s in symbols]
        print(json.dumps(output, indent=2, default=str))
