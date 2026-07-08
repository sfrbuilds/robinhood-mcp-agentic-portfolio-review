"""
portfolio_review.py
===================
Full pipeline: live portfolio + technicals + macro context → Claude written review
+ structured actions JSON for optional Robinhood MCP execution.

Step 1 — fetch live positions (run once, saves positions.json):
    claude -p research/fetch_positions.md > positions.json

Step 2 — run the review:
    python research/portfolio_review.py
    python research/portfolio_review.py --mock        # use built-in sample portfolio for testing
    python research/portfolio_review.py --mock --actions  # also write actions.json

Step 3 — (optional) execute approved actions via Robinhood MCP:
    # Edit actions.json, set ready_to_execute: true on any action you approve
    claude -p research/execute_actions.md

Flags:
    --positions <path>   path to positions JSON (default: positions.json)
    --mock               use sample portfolio, no file needed
    --actions            also generate actions.json (structured Robinhood MCP calls)
    --actions-out <path> path to write actions JSON (default: actions.json)
    --no-macro           skip market context fetch (faster)
    --json               dump full data payload to stdout instead of review
"""

import sys
import os
import json
import math
import subprocess
import textwrap
from datetime import date, datetime, timezone, timedelta

# ── Dependency check ──────────────────────────────────────────────────────────
try:
    import anthropic
except ImportError:
    print("Missing dependency: pip install anthropic")
    sys.exit(1)

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
except ImportError:
    print("Missing dependency: pip install yfinance pandas numpy")
    sys.exit(1)

# Add repo root to path so we can import ticker_analysis
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.ticker_analysis import analyze, market_context

# ── Constants ─────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-6"

# ── Live trading flag ─────────────────────────────────────────────────────────
# Set LIVE_TRADING=true in .env or pass --live flag to enable real execution.
# When false (default): full dry run — review + actions.json, nothing sent to Robinhood.
# When true: engine-mandated SELL exits auto-approved and sent to Robinhood MCP.
# BUY actions are NEVER auto-approved regardless of this flag.
LIVE_TRADING = os.environ.get("LIVE_TRADING", "false").lower() == "true"

# Engine risk parameters (must match CLAUDE.md)
VIX_HARD_BLOCK       = 40
VIX_SIZE_DOWN        = 25
MIN_RSI              = 45
MAX_RSI              = 70
MIN_REL_VOL          = 1.3

MOCK_PORTFOLIO = {
    "account": {
        "equity":        25000.00,
        "cash":          3200.00,
        "buying_power":  3200.00,
    },
    "positions": [
        {"symbol": "AAPL",  "quantity": 15,  "avg_cost": 265.00, "current_price": None, "current_value": None, "unrealized_pl": None, "unrealized_pl_pct": None},
        {"symbol": "NVDA",  "quantity": 8,   "avg_cost": 195.00, "current_price": None, "current_value": None, "unrealized_pl": None, "unrealized_pl_pct": None},
        {"symbol": "MSFT",  "quantity": 10,  "avg_cost": 430.00, "current_price": None, "current_value": None, "unrealized_pl": None, "unrealized_pl_pct": None},
    ],
}


# ── Market hours ─────────────────────────────────────────────────────────────

def market_status() -> dict:
    """
    Return current US equity market status and execution timing language.
    All times in US/Eastern (ET). No external dependency — pure datetime math.
    """
    ET = timezone(timedelta(hours=-4))  # EDT (UTC-4); change to -5 in winter (EST)
    now_et = datetime.now(ET)
    weekday = now_et.weekday()  # 0=Mon, 6=Sun

    t = now_et.time()
    from datetime import time as dtime

    PRE_OPEN   = dtime(4,  0)
    OPEN       = dtime(9, 30)
    CLOSE      = dtime(16, 0)
    AH_CLOSE   = dtime(20, 0)

    # Next regular open as a human string
    def _next_open_str():
        days_ahead = 1
        candidate = now_et + timedelta(days=days_ahead)
        while candidate.weekday() >= 5:  # skip weekend
            days_ahead += 1
            candidate = now_et + timedelta(days=days_ahead)
        day_name = candidate.strftime("%A %Y-%m-%d")
        return f"{day_name} at 9:30 AM ET"

    is_weekend = weekday >= 5

    if is_weekend:
        state = "closed_weekend"
        is_open = False
        exec_language = f"market is closed (weekend). Execute at next open: {_next_open_str()}"
        urgency_now   = f"at next open ({_next_open_str()})"
    elif t < PRE_OPEN:
        state = "closed_overnight"
        is_open = False
        exec_language = f"market is closed. Pre-market opens at 4:00 AM ET. Regular session opens 9:30 AM ET today."
        urgency_now   = "at today's open (9:30 AM ET)"
    elif t < OPEN:
        state = "pre_market"
        is_open = False
        mins_to_open = int(((OPEN.hour * 60 + OPEN.minute) - (t.hour * 60 + t.minute)))
        exec_language = f"pre-market session active. Regular session opens in ~{mins_to_open} minutes (9:30 AM ET)."
        urgency_now   = "at today's open (9:30 AM ET)"
    elif t < CLOSE:
        state = "open"
        is_open = True
        mins_left = int(((CLOSE.hour * 60 + CLOSE.minute) - (t.hour * 60 + t.minute)))
        exec_language = f"market is OPEN. {mins_left} minutes remaining in regular session."
        urgency_now   = "immediately (market is open NOW)"
    elif t < AH_CLOSE:
        state = "after_hours"
        is_open = False
        exec_language = f"after-hours session active (closes 8:00 PM ET). Regular session resumes {_next_open_str()}."
        urgency_now   = f"at next open ({_next_open_str()})"
    else:
        state = "closed_overnight"
        is_open = False
        exec_language = f"market is closed. Next regular session: {_next_open_str()}."
        urgency_now   = f"at next open ({_next_open_str()})"

    return {
        "state":           state,       # open | pre_market | after_hours | closed_overnight | closed_weekend
        "is_open":         is_open,
        "time_et":         now_et.strftime("%Y-%m-%d %H:%M ET"),
        "exec_language":   exec_language,
        "urgency_now":     urgency_now,  # natural language for use in prompts
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pct(new, old):
    if not old or old == 0:
        return None
    return round((new - old) / abs(old) * 100, 2)


def load_positions(path: str) -> dict:
    with open(path) as f:
        raw = f.read().strip()
    # claude -p sometimes wraps output in markdown fences
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
    if raw.endswith("```"):
        raw = "\n".join(raw.split("\n")[:-1])
    return json.loads(raw.strip())


def enrich_positions(portfolio: dict) -> dict:
    """Fill in current_price / current_value / P&L from yfinance if missing."""
    for pos in portfolio["positions"]:
        sym = pos["symbol"]
        if pos.get("current_price"):
            continue
        try:
            h = yf.Ticker(sym).history(period="2d", auto_adjust=True)
            if h is not None and len(h):
                price = float(h["Close"].iloc[-1])
                pos["current_price"]       = round(price, 4)
                pos["current_value"]       = round(price * pos["quantity"], 2)
                pos["unrealized_pl"]       = round((price - pos["avg_cost"]) * pos["quantity"], 2)
                pos["unrealized_pl_pct"]   = round(_pct(price, pos["avg_cost"]), 2)
        except Exception:
            pass
    return portfolio


def engine_check(pos: dict, ta: dict) -> dict:
    """
    Run the 4-condition engine signal check against a position.
    Returns a dict with pass/fail per condition and an overall verdict.
    """
    t = ta.get("technicals", {})
    macd  = t.get("macd", {})
    rsi   = t.get("rsi_14")
    ema20 = t.get("above_ema_20")
    rel_v = t.get("volume", {}).get("rel_vol")
    gc    = t.get("golden_cross")

    # For existing positions we check if original entry conditions STILL hold
    # (not requiring a fresh crossover — that's for new entries only)
    checks = {
        "macd_bullish":    macd.get("trend") == "bullish",
        "rsi_in_range":    rsi is not None and MIN_RSI <= rsi <= MAX_RSI,
        "above_ema_20":    bool(ema20),
        "golden_cross":    bool(gc),
    }
    # Volume only required for NEW entries; not a hold/exit signal
    passing = sum(checks.values())
    verdict = "hold" if passing >= 3 else "review"
    if passing <= 1:
        verdict = "exit_candidate"

    # ATR-based stop
    atr = t.get("atr_14")
    price = ta.get("price", {}).get("current")
    stop = round(price - 1.5 * atr, 2) if atr and price else None
    target = round(price + 2.5 * atr, 2) if atr and price else None
    pct_to_stop = round(_pct(stop, price), 2) if stop and price else None

    return {
        "checks":      checks,
        "passing":     passing,
        "verdict":     verdict,           # "hold" | "review" | "exit_candidate"
        "stop":        stop,
        "target":      target,
        "pct_to_stop": pct_to_stop,
    }


def build_payload(portfolio: dict, no_macro: bool = False) -> dict:
    """Assemble the full data payload for the Claude synthesis call."""
    symbols = [p["symbol"] for p in portfolio["positions"]]

    print(f"[portfolio_review] Fetching technicals for {symbols}...", file=sys.stderr)
    analyses = {sym: analyze(sym) for sym in symbols}

    macro = {}
    if not no_macro:
        print("[portfolio_review] Fetching market context...", file=sys.stderr)
        # Include sector ETFs for positions' sectors
        extra_etfs = []
        from research.ticker_analysis import _SECTOR_ETF
        for sym, ta in analyses.items():
            sector = ta.get("meta", {}).get("sector", "")
            etf = _SECTOR_ETF.get(sector)
            if etf and etf not in extra_etfs:
                extra_etfs.append(etf)
        macro = market_context(extra_sectors=extra_etfs)

    # Engine check per position
    position_data = []
    for pos in portfolio["positions"]:
        sym = pos["symbol"]
        ta  = analyses.get(sym, {})
        ec  = engine_check(pos, ta)
        position_data.append({
            "position":      pos,
            "analysis":      ta,
            "engine_check":  ec,
        })

    return {
        "as_of":     date.today().isoformat(),
        "account":   portfolio.get("account", {}),
        "positions": position_data,
        "macro":     macro,
    }


# ── Prompt assembly ───────────────────────────────────────────────────────────

def build_prompt(payload: dict) -> str:
    acct    = payload["account"]
    macro   = payload["macro"]
    spy     = macro.get("spy", {})
    vix     = macro.get("vix", {})
    sectors = macro.get("sectors", {})
    flags   = macro.get("regime_flags", [])
    mkt     = payload.get("market_status", market_status())

    lines = [
        f"Today is {payload['as_of']}. Current time: {mkt['time_et']}.",
        f"Market status: {mkt['exec_language']}",
        f"When recommending trade execution, use this timing: {mkt['urgency_now']}.",
        "Do NOT say 'at the open tomorrow' if the market is currently open.",
        "",
        "## Engine rules (enforce strictly)",
        f"- VIX hard block >= {VIX_HARD_BLOCK}; elevated >= {VIX_SIZE_DOWN} (size down)",
        "- Entry requires: MACD bullish + RSI 45-70 + price above EMA20 + golden cross",
        "- Position hold requires >= 3 of 4 conditions (volume not required for existing holds)",
        "- Exit candidates: <= 1 of 4 conditions passing",
        "",
        "## Account",
        f"  Equity: ${acct.get('equity', 'n/a'):,}",
        f"  Cash:   ${acct.get('cash', 'n/a'):,}  (buying power: ${acct.get('buying_power', 'n/a'):,})",
        f"  Positions: {len(payload['positions'])}",
        "",
    ]

    if spy:
        lines += [
            "## Market regime",
            f"  SPY:  ${spy.get('price', 'n/a')}  day {spy.get('day_change_pct', 'n/a')}%  "
            f"30d {spy.get('return_30d_pct', 'n/a')}%  YTD {spy.get('return_ytd_pct', 'n/a')}%",
            f"  SPY vs EMA20: {'ABOVE (regime OK)' if spy.get('above_ema_20') else 'BELOW (entries blocked)'}",
            f"  Golden cross: {'YES' if spy.get('golden_cross') else 'NO (death cross)'}",
            f"  VIX: {vix.get('current', 'n/a')}  (30d avg {vix.get('avg_30d', 'n/a')})"
            + ("  *** VIX HARD BLOCK ***" if vix.get("hard_block") else ""),
        ]
        if flags:
            lines.append("  Regime checks: " + " | ".join(flags))
        if sectors:
            sorted_s = sorted(sectors.items(), key=lambda x: x[1].get("return_30d_pct") or -999, reverse=True)
            lines.append("  Sector 30d: " + "  ".join(
                f"{etf} {d.get('return_30d_pct', 'n/a')}% ({'▲' if d.get('trend') == 'bullish' else '▼'})"
                for etf, d in sorted_s
            ))
        lines.append("")

    lines.append("## Positions")
    for item in payload["positions"]:
        pos = item["position"]
        ta  = item["analysis"]
        ec  = item["engine_check"]
        t   = ta.get("technicals", {})
        p   = ta.get("price", {})
        f   = ta.get("fundamentals", {})
        a   = ta.get("analyst", {})
        perf= ta.get("performance", {})
        macd= t.get("macd", {})

        sym = pos["symbol"]
        lines += [
            f"",
            f"### {sym}  —  {ta.get('meta', {}).get('name', '')}",
            f"  Entry avg: ${pos['avg_cost']}  |  Current: ${p.get('current', 'n/a')}  |  "
            f"P&L: ${pos.get('unrealized_pl', 'n/a')} ({pos.get('unrealized_pl_pct', 'n/a')}%)",
            f"  Shares: {pos['quantity']}  |  Value: ${pos.get('current_value', 'n/a')}",
            f"  7d: {perf.get('7d', {}).get('return_pct', 'n/a')}%  "
            f"30d: {perf.get('30d', {}).get('return_pct', 'n/a')}%  "
            f"YTD: {perf.get('ytd', {}).get('return_pct', 'n/a')}%  "
            f"1Y: {perf.get('1y', {}).get('return_pct', 'n/a')}%",
            f"  RSI: {t.get('rsi_14', 'n/a')}  "
            f"MACD trend: {macd.get('trend', 'n/a')}  "
            f"cross: {macd.get('crossover', 'n/a')}"
            + (f" ({macd.get('days_since_cross')}d ago)" if macd.get('days_since_cross') else ""),
            f"  EMA20: {'above' if t.get('above_ema_20') else 'BELOW'}  "
            f"EMA50: {'above' if t.get('above_ema_50') else 'BELOW'}  "
            f"EMA200: {'above' if t.get('above_ema_200') else 'BELOW'}  "
            f"Golden cross: {'YES' if t.get('golden_cross') else 'NO'}",
            f"  ATR stop: ${ec.get('stop', 'n/a')} ({ec.get('pct_to_stop', 'n/a')}% away)  "
            f"Target: ${ec.get('target', 'n/a')}",
            f"  P/E fwd: {f.get('pe_forward', 'n/a')}  "
            f"Rev growth: {f.get('revenue_growth_yoy', 'n/a')}%  "
            f"Analyst PT: ${a.get('price_target_avg', 'n/a')} ({a.get('price_target_upside', 'n/a')}% up)  "
            f"Consensus: {a.get('consensus', 'n/a')}",
            f"  ** ENGINE: {ec['verdict'].upper()}  ({ec['passing']}/4 conditions passing) **",
            f"     Checks: MACD bullish={ec['checks']['macd_bullish']}  "
            f"RSI in range={ec['checks']['rsi_in_range']}  "
            f"Above EMA20={ec['checks']['above_ema_20']}  "
            f"Golden cross={ec['checks']['golden_cross']}",
        ]
        if ta.get("signal_flags"):
            lines.append("  Flags: " + " | ".join(ta["signal_flags"]))

    lines += [
        "",
        "## Your task",
        "Write a tight portfolio review. Format:",
        "",
        "**OVERALL POSTURE** (2-3 sentences: macro read + portfolio health score X/10)",
        "",
        "**POSITION REVIEW**",
        "For each position: one sentence on the technical/fundamental picture, "
        "then one clear line — HOLD / REDUCE / EXIT — with the specific reason based "
        "on engine rules and current context. If exit, state at what level or condition.",
        "",
        "**TOP ACTION ITEMS** (max 3, numbered, specific and actionable)",
        "",
        "**CASH DEPLOYMENT** (if any: is there a setup worth entering now? if not, say so explicitly)",
        "",
        "Be direct. No hedging. If the data doesn't support a strong view, say that. "
        "Flag any positions at or near their ATR-based stop. "
        "Reference specific prices, percentages, and indicator readings.",
    ]

    return "\n".join(lines)


# ── Claude synthesis ──────────────────────────────────────────────────────────

def _client() -> "anthropic.Anthropic":
    api_key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set.")
    return anthropic.Anthropic(api_key=api_key)


def synthesize(prompt: str) -> str:
    try:
        msg = _client().messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
    except ValueError as e:
        return f"[ERROR] {e}"


# ── Structured actions generation ─────────────────────────────────────────────
#
# Uses Claude tool_use (forced) so the output is always valid JSON matching
# the schema below — no regex parsing or hope-based JSON extraction.
#
# The order_params block inside each action is shaped to match the Robinhood
# Trading MCP place_equity_order tool exactly. Verify parameter names against
# your connected MCP schema before enabling live execution.

ACTIONS_TOOL = {
    "name": "output_portfolio_actions",
    "description": (
        "Output the structured list of recommended portfolio actions "
        "derived from the engine analysis. Every position must appear "
        "in either 'actions' (if something should change) or 'no_action'. "
        "ready_to_execute must always be false — the human sets this."
    ),
    "input_schema": {
        "type": "object",
        "required": ["portfolio_health_score", "regime_summary", "actions", "no_action"],
        "properties": {
            "portfolio_health_score": {
                "type": "integer",
                "description": "Overall portfolio health 1-10 based on engine conditions"
            },
            "regime_summary": {
                "type": "object",
                "required": ["spy_above_ema20", "vix", "new_entries_blocked", "note"],
                "properties": {
                    "spy_above_ema20":    {"type": "boolean"},
                    "vix":               {"type": "number"},
                    "new_entries_blocked":{"type": "boolean"},
                    "note":              {"type": "string"}
                }
            },
            "actions": {
                "type": "array",
                "description": "Positions requiring a change (sell, reduce, stop-loss order)",
                "items": {
                    "type": "object",
                    "required": [
                        "action_id", "symbol", "action_type", "urgency",
                        "rationale", "conditions_passing",
                        "order_params", "risk_context", "ready_to_execute"
                    ],
                    "properties": {
                        "action_id":          {"type": "string", "description": "e.g. action_001"},
                        "symbol":             {"type": "string"},
                        "action_type":        {"type": "string", "enum": ["SELL", "SELL_PARTIAL", "BUY", "SET_STOP"]},
                        "urgency":            {"type": "string", "enum": ["immediate", "today", "this_week"]},
                        "rationale":          {"type": "string", "description": "Concise reason citing specific indicators"},
                        "conditions_passing": {"type": "integer", "description": "Engine conditions currently passing (0-4)"},
                        "order_params": {
                            "type": "object",
                            "description": "Ready-to-use parameters for Robinhood MCP place_equity_order",
                            "required": ["symbol", "side", "order_type", "time_in_force"],
                            "properties": {
                                "symbol":        {"type": "string"},
                                "side":          {"type": "string", "enum": ["buy", "sell"]},
                                "order_type":    {"type": "string", "enum": ["market", "limit"]},
                                "quantity":      {"type": "number", "description": "Shares. Required unless using notional."},
                                "notional":      {"type": "number", "description": "Dollar amount. Use instead of quantity for fractional."},
                                "limit_price":   {"type": "number", "description": "Required if order_type is limit"},
                                "time_in_force": {"type": "string", "enum": ["gfd", "gtc"]}
                            }
                        },
                        "risk_context": {
                            "type": "object",
                            "required": ["current_price", "atr_stop", "unrealized_pl_pct"],
                            "properties": {
                                "current_price":      {"type": "number"},
                                "atr_stop":           {"type": "number"},
                                "unrealized_pl_pct":  {"type": "number"},
                                "estimated_proceeds": {"type": "number"}
                            }
                        },
                        "ready_to_execute": {
                            "type": "boolean",
                            "description": "Always false. Human must manually set to true to enable execution."
                        }
                    }
                }
            },
            "no_action": {
                "type": "array",
                "description": "Positions with no recommended change",
                "items": {
                    "type": "object",
                    "required": ["symbol", "rationale", "conditions_passing", "watch_level"],
                    "properties": {
                        "symbol":             {"type": "string"},
                        "rationale":          {"type": "string"},
                        "conditions_passing": {"type": "integer"},
                        "watch_level":        {"type": "string", "description": "Price level that would trigger re-evaluation"}
                    }
                }
            }
        }
    }
}


def generate_actions(payload: dict) -> dict:
    """
    Call Claude with tool_use forced to get a structured actions dict.
    Returns the parsed tool input — always valid against ACTIONS_TOOL schema.
    """
    # Build a compact version of the payload for the actions prompt
    acct  = payload["account"]
    macro = payload["macro"]
    spy   = macro.get("spy", {})
    vix   = macro.get("vix", {})
    mkt   = payload.get("market_status", market_status())

    # Urgency semantics depend on market state
    if mkt["is_open"]:
        urgency_immediate = "immediate: market is open, execute now"
        urgency_today     = "today: execute before close"
        tif               = "gfd"
    else:
        urgency_immediate = f"immediate: execute at next open ({mkt['urgency_now']})"
        urgency_today     = f"today: execute at next open ({mkt['urgency_now']})"
        tif               = "gfd"

    lines = [
        f"Portfolio analysis as of {payload['as_of']}. Current time: {mkt['time_et']}.",
        f"Market status: {mkt['exec_language']}",
        f"Equity: ${acct.get('equity', 'n/a')}  Cash: ${acct.get('cash', 'n/a')}",
        f"SPY above EMA20: {spy.get('above_ema20', spy.get('above_ema_20', 'unknown'))}",
        f"VIX: {vix.get('current', 'n/a')}",
        "",
        "Engine rules: HOLD requires 3+ of 4 conditions (MACD bullish / RSI 45-70 / above EMA20 / golden cross). "
        "EXIT CANDIDATE = 0-1 passing. REVIEW = 2 passing.",
        "",
        "Positions:"
    ]

    for item in payload["positions"]:
        pos = item["position"]
        ta  = item["analysis"]
        ec  = item["engine_check"]
        p   = ta.get("price", {})

        lines.append(
            f"  {pos['symbol']}: qty={pos['quantity']} avg_cost=${pos['avg_cost']} "
            f"current=${p.get('current', 'n/a')} "
            f"pl_pct={pos.get('unrealized_pl_pct', 'n/a')}% "
            f"engine={ec['verdict']} conditions={ec['passing']}/4 "
            f"atr_stop=${ec.get('stop', 'n/a')}"
        )

    lines += [
        "",
        "Generate structured portfolio actions. Rules:",
        "- 0-1 conditions passing → action_type SELL (full exit) unless fundamental override is exceptionally strong",
        "- 2 conditions passing → action_type SELL_PARTIAL (reduce by 50%) or SELL",
        "- 3-4 conditions passing → no_action",
        "- If SPY is below EMA20, no BUY actions regardless of individual signals",
        "- order_params must be complete and ready to pass directly to Robinhood MCP place_equity_order",
        f"- For market sells: time_in_force = {tif}",
        "- ready_to_execute must always be false",
        f"- urgency meanings given current market state:",
        f"    immediate = {urgency_immediate}",
        f"    today     = {urgency_today}",
        f"    this_week = within 5 trading days",
    ]

    prompt = "\n".join(lines)

    try:
        msg = _client().messages.create(
            model=MODEL,
            max_tokens=2048,
            tools=[ACTIONS_TOOL],
            tool_choice={"type": "tool", "name": "output_portfolio_actions"},
            messages=[{"role": "user", "content": prompt}],
        )

        # Extract the tool use block
        for block in msg.content:
            if block.type == "tool_use" and block.name == "output_portfolio_actions":
                result = block.input
                result["generated_at"] = payload["as_of"]
                result["execution_note"] = (
                    "Set ready_to_execute: true on any action you approve, "
                    "then run: claude -p research/execute_actions.md"
                )
                return result

        return {"error": "No tool_use block returned", "generated_at": payload["as_of"]}

    except ValueError as e:
        return {"error": str(e), "generated_at": payload["as_of"]}
    except Exception as e:
        return {"error": f"Unexpected error: {e}", "generated_at": payload["as_of"]}


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args         = sys.argv[1:]
    use_mock     = "--mock"        in args
    no_macro     = "--no-macro"    in args
    dump_json    = "--json"        in args
    # --live overrides .env; --dry-run forces dry run regardless of .env
    live_trading = (LIVE_TRADING or "--live" in args) and "--dry-run" not in args

    positions_path = "positions.json"
    if "--positions" in args:
        idx = args.index("--positions")
        positions_path = args[idx + 1]

    actions_out = "actions.json"
    if "--actions-out" in args:
        idx = args.index("--actions-out")
        actions_out = args[idx + 1]

    # Load portfolio
    if use_mock:
        print("[portfolio_review] Using mock portfolio.", file=sys.stderr)
        portfolio = MOCK_PORTFOLIO
    else:
        if not os.path.exists(positions_path):
            print(
                f"[portfolio_review] '{positions_path}' not found.\n\n"
                "Fetch live positions first:\n"
                "  claude -p research/fetch_positions.md > positions.json\n\n"
                "Or test with mock data:\n"
                "  python research/portfolio_review.py --mock",
                file=sys.stderr
            )
            sys.exit(1)
        portfolio = load_positions(positions_path)

    # Enrich with live prices if missing
    portfolio = enrich_positions(portfolio)

    # Capture market status once (used in both prompts)
    mkt = market_status()
    print(f"[portfolio_review] Market status: {mkt['exec_language']}", file=sys.stderr)
    print(f"[portfolio_review] Live trading: {'ENABLED' if live_trading else 'disabled (dry run)'}", file=sys.stderr)

    # Build full payload
    payload = build_payload(portfolio, no_macro=no_macro)
    payload["market_status"] = mkt

    if dump_json:
        print(json.dumps(payload, indent=2, default=str))
        sys.exit(0)

    # ── Written review ────────────────────────────────────────────────────────
    prompt = build_prompt(payload)
    print("[portfolio_review] Calling Claude for written review...\n", file=sys.stderr)
    review = synthesize(prompt)

    print("=" * 60)
    print(f"PORTFOLIO REVIEW  |  {payload['as_of']}")
    print(f"{'LIVE TRADING ENABLED' if live_trading else 'DRY RUN'}")
    print("=" * 60)
    print()
    print(review)
    print()

    # ── Structured actions (always generated) ────────────────────────────────
    print("[portfolio_review] Generating structured actions...", file=sys.stderr)
    actions = generate_actions(payload)

    if actions.get("error"):
        print(f"[portfolio_review] Error generating actions: {actions['error']}", file=sys.stderr)
        sys.exit(1)

    # When live: auto-approve SELL/SELL_PARTIAL with immediate urgency.
    # BUY actions are NEVER auto-approved — that requires a separate live buy flag (not built yet).
    approved_count = 0
    if live_trading:
        for a in actions.get("actions", []):
            if a.get("action_type") in ("SELL", "SELL_PARTIAL") and a.get("urgency") == "immediate":
                a["ready_to_execute"] = True
                approved_count += 1
        if approved_count == 0:
            print("[portfolio_review] Live trading on but no immediate sell actions to approve.", file=sys.stderr)

    with open(actions_out, "w") as f:
        json.dump(actions, f, indent=2, default=str)

    # ── Print action summary ──────────────────────────────────────────────────
    print(f"{'─'*60}")
    print(f"ACTIONS  {'(LIVE: executing now)' if live_trading and approved_count else '(DRY RUN: review actions.json)'}")
    print(f"{'─'*60}")

    regime = actions.get("regime_summary", {})
    print(f"Health: {actions.get('portfolio_health_score', '?')}/10  |  "
          f"Entries blocked: {regime.get('new_entries_blocked', '?')}  |  "
          f"{regime.get('note', '')}")
    print()

    acts = actions.get("actions", [])
    if acts:
        for a in acts:
            op  = a.get("order_params", {})
            qty = op.get("quantity") or f"${op.get('notional', '?')}"
            approved = a.get("ready_to_execute", False)
            status = "APPROVED" if approved else "pending approval"
            print(
                f"  {'>>>' if approved else '   '} "
                f"{a['action_type']} {a['symbol']}  "
                f"qty={qty}  {op.get('order_type','?')}  "
                f"tif={op.get('time_in_force','?')}  [{status}]"
            )
            for ln in textwrap.wrap(a.get('rationale', ''), width=100, initial_indent='       ', subsequent_indent='       '):
                print(ln)
    else:
        print("  No actions recommended. All positions holding.")

    no_act = actions.get("no_action", [])
    if no_act:
        print()
        for n in no_act:
            header = f"  --- {n['symbol']}: "
            for ln in textwrap.wrap(header + n.get('rationale', ''), width=100, subsequent_indent=' ' * len(header)):
                print(ln)

    print()

    # ── Execute if live ───────────────────────────────────────────────────────
    if live_trading and approved_count > 0:
        print(f"[portfolio_review] Handing {approved_count} approved action(s) to Robinhood MCP...", file=sys.stderr)
        execute_prompt = os.path.join(os.path.dirname(__file__), "execute_actions.md")
        result = subprocess.run(
            ["claude", "-p", execute_prompt],
            capture_output=False,   # let execution output print directly
            text=True,
        )
        if result.returncode != 0:
            print(f"[portfolio_review] Execution returned non-zero exit code: {result.returncode}", file=sys.stderr)
    elif live_trading and approved_count == 0:
        print("No approved actions to execute.")
    else:
        # Dry run — show next steps
        print(f"Saved to: {actions_out}")
        print()
        print("To execute with live trading:")
        print(f"  LIVE_TRADING=true python research/portfolio_review.py")
        print("  or:")
        print(f"  python research/portfolio_review.py --live")
        print()
        print("To manually approve specific actions:")
        print(f"  1. Edit {actions_out}: set ready_to_execute: true on what you want")
        print(f"  2. claude -p research/execute_actions.md")
