# robinhood-mcp-agentic-portfolio-review

A rules-based equity portfolio screening tool, built natively on the [Robinhood Agentic Trading MCP](https://robinhood.com/us/en/support/articles/agentic-trading-overview/).

Run a daily portfolio review. Get a written assessment of every position with explicit hold or exit-consideration flags. Optionally execute engine-flagged exits through Robinhood — dry-run by default, always auditable.

**Scope:** position screening and exit decisions only. No buy signals, no entry automation, no forced holding periods, no market-regime-triggered exits. That's intentional.

---

## What it does

1. Pulls your live Robinhood positions via the MCP
2. Fetches technicals and fundamentals for every holding (MACD, RSI, EMAs, ATR, Bollinger Bands, P/E, analyst targets, and more)
3. Fetches macro context: SPY regime, VIX, sector ETF trends — displayed in the written review for situational awareness, not used to trigger exits
4. Runs each position through a 4-condition engine signal check
5. Calls Claude (Sonnet) for a written portfolio review with explicit hold/exit calls
6. Generates a structured `actions.json` of recommended trades, shaped for Robinhood MCP execution
7. Optionally executes approved exits automatically when `LIVE_TRADING=true`

---

## The 4-condition engine

A position is healthy if at least 3 of these 4 hold:

| # | Condition | Why |
|---|-----------|-----|
| 1 | MACD trend bullish | Momentum is with you |
| 2 | RSI between 45 and 70 | Not weak, not overbought |
| 3 | Price above EMA20 | Short-term trend aligned |
| 4 | Golden cross (EMA50 > EMA200) | Long-term trend aligned |

**0–1 conditions passing → flagged as an exit candidate in the written review.** In default dry-run mode, nothing is sent to Robinhood. When `LIVE_TRADING` is enabled, engine-flagged immediate exits (`SELL` / `SELL_PARTIAL`) are auto-approved and executed. Actions with `urgency: today` or `this_week` are never auto-approved — those go through the manual approval path.

---

## Repository structure

```
robinhood-mcp-agentic-portfolio-review/
├── research/
│   ├── ticker_analysis.py      # technicals + fundamentals via yfinance
│   ├── portfolio_review.py     # main pipeline — review + actions.json
│   ├── fetch_positions.md      # claude -p prompt: pulls live Robinhood positions
│   └── execute_actions.md      # claude -p prompt: executes approved actions via Robinhood MCP
├── scripts/
│   ├── run_review.sh           # orchestrator: fetch positions → review → notify
│   ├── market_schedule.py      # NYSE trading day detection
│   └── notify.py               # Telegram delivery
├── deploy/
│   ├── SETUP.md                # one-time EC2 setup guide
│   └── crontab.example         # 9:45 AM + 3:45 PM ET, TZ-aware
├── .env.example
└── requirements.txt
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Required: ANTHROPIC_API_KEY
# Optional: LIVE_TRADING=true to enable automated execution
```

### 3. Set up the Robinhood Agentic Trading MCP

Robinhood's Agentic Trading is a dedicated account type where an AI agent can place trades programmatically via MCP (Model Context Protocol). It requires a separate Robinhood Agentic account.

#### Which AI agent?

The Robinhood Trading MCP is a standard MCP server — it works with any MCP-compatible agent: Claude Code, OpenAI Codex, Cursor, Windsurf, or any agent framework that supports the MCP protocol. This repo is built on **Claude Code**, but swapping the agent layer is straightforward since all execution logic lives in the `.md` prompt files.

#### Connect the MCP to Claude Code

**Step 1: Add the MCP server**

```bash
claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading
```

**Step 2: Authenticate**

Open Claude Code and run:

```
/mcp
```

Select `robinhood-trading` and press **Authenticate**. This opens a Robinhood OAuth flow in your browser. If you don't have an Agentic Trading account, Robinhood will prompt you to create one during this flow.

**Step 3: Verify the connection**

```
/mcp
```

`robinhood-trading` should show as `✓ connected` with tools listed (get_account, list_positions, place_equity_order, etc.).

> **Note:** The MCP uses OAuth — your credentials are never stored in plaintext anywhere in this repo. The session token is managed by Claude Code.

---

## Daily run

```bash
./scripts/run_review.sh
```

That's it. The script handles everything in sequence: checks it's a trading day, pulls live positions via the Robinhood MCP, runs the analysis pipeline, and sends results to Telegram. To enable automated execution of engine-flagged exits, set `LIVE_TRADING=true` in your `.env`.

Output:
- Written portfolio review (stdout + Telegram)
- `actions.json` with structured trade recommendations

**Under the hood** (for debugging or running components individually):

```bash
# Pull positions manually
claude -p research/fetch_positions.md > positions.json

# Run pipeline against existing positions.json
python research/portfolio_review.py           # dry run
python research/portfolio_review.py --live    # execute immediate exits
python research/portfolio_review.py --mock    # test with mock portfolio, no MCP needed
```

---

## Live trading

`LIVE_TRADING` controls whether the pipeline executes exits automatically.

| Mode | How to enable | Behavior |
|------|--------------|----------|
| Dry run | Default | Review + actions.json only. Nothing sent to Robinhood. |
| Live (one-time) | `--live` flag | Immediate engine-exit sells auto-approved and executed. |
| Live (persistent) | `LIVE_TRADING=true` in `.env` | Same as above on every run. |
| Force dry run | `--dry-run` | Overrides .env regardless of `LIVE_TRADING` setting. |

**What gets auto-approved when live:**
- `SELL` or `SELL_PARTIAL` actions with `urgency: immediate`
- These are positions at 0–1 conditions — engine rule violations, not judgment calls

**What never gets auto-approved:**
- `BUY` actions (no buy path exists in this version)
- Actions with `urgency: today` or `this_week`

### Manual approval path

If you want to approve specific actions individually:

```bash
# 1. Generate review without executing
python research/portfolio_review.py

# 2. Open actions.json, set ready_to_execute: true on what you want
# 3. Hand off to Robinhood MCP
claude -p research/execute_actions.md
```

The execute prompt calls `review_equity_order` (dry-run preview) before `place_equity_order` on every action.

---

## Exit thresholds

ATR is fetched for each position and used to compute drawdown and profit levels relative to your actual entry price.

| Threshold | Value | What it means in review |
|---|---|---|
| Stop level | 1.5 × ATR below entry | Position flagged for exit consideration if price has drawn down to this level |
| Profit target | 2.5 × ATR above entry | Position near target — review may suggest trimming or full exit |

These are advisory thresholds displayed in the written review. The exit decision is always yours unless `LIVE_TRADING` is enabled, in which case only 0–1 condition positions (immediate urgency) are auto-executed — not target/stop proximity alone.

---

## Disclaimer

This is for informational and research purposes. Nothing here is financial advice. Use real money at your own risk.
