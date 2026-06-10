# Cloud Deployment Setup

This guide covers deploying the automated portfolio review to a Linux server (EC2 or equivalent).
The pipeline runs twice daily at 9:45 AM and 3:45 PM ET on trading days via cron.

---

## Prerequisites

- A Linux server (Amazon Linux 2023, Ubuntu 22+)
- SSH access
- An Anthropic API key
- A Robinhood Agentic Trading account

---

## One-time server setup

### 1. Clone the repo

```bash
git clone https://github.com/your-username/robinhood-mcp-agentic-portfolio-review.git
cd robinhood-mcp-agentic-portfolio-review
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
pip install pandas_market_calendars   # full NYSE holiday calendar
```

### 3. Configure environment

```bash
cp .env.example .env
nano .env
```

Required:
```
ANTHROPIC_API_KEY=sk-ant-...
LIVE_TRADING=false             # set true when ready for live execution
TELEGRAM_BOT_TOKEN=...         # optional but strongly recommended for cloud
TELEGRAM_CHAT_ID=...
```

### 4. Install Claude Code

Claude Code is the agent runtime that bridges the Python pipeline to the Robinhood MCP.

```bash
npm install -g @anthropic-ai/claude-code
```

Verify:
```bash
claude --version
```

### 5. Authenticate the Robinhood MCP

This step is interactive and only needs to be done once. The auth token persists in `~/.claude/` and is reused by all subsequent headless `claude -p` calls.

```bash
# Add the MCP server
claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading

# Open the interactive session to authenticate
claude
```

Inside Claude Code:
```
/mcp
```

Select `robinhood-trading` → press **Authenticate** → complete the Robinhood OAuth flow in your browser → confirm the connection shows `✓ connected`.

Exit Claude Code. The session token is now stored on disk.

**Test that headless access works:**

```bash
claude -p research/fetch_positions.md
```

You should see JSON output with your positions. If you see an auth error, repeat the interactive auth step.

### 6. Make the run script executable

```bash
chmod +x scripts/run_review.sh
```

### 7. Test a manual run

```bash
./scripts/run_review.sh morning
```

Check `reviews/` for the output file.

---

## Cron setup

```bash
crontab -e
```

Paste the contents of `deploy/crontab.example`, updating the `REPO` path to match your server.

```
TZ=America/New_York
MAILTO=""

REPO=/home/ec2-user/robinhood-mcp-agentic-portfolio-review

45 9  * * 1-5 $REPO/scripts/run_review.sh morning   >> $REPO/logs/cron.log 2>&1
45 15 * * 1-5 $REPO/scripts/run_review.sh afternoon >> $REPO/logs/cron.log 2>&1
```

Verify cron is running:

```bash
crontab -l
sudo systemctl status cron    # Ubuntu
sudo systemctl status crond   # Amazon Linux
```

---

## How the MCP auth token persists

`claude -p` (headless mode) reuses the OAuth token stored in `~/.claude/` from the interactive session.
Robinhood's token TTL is not publicly documented — if automated runs start failing with auth errors,
re-run the interactive setup (Step 5) to refresh the token.

The `run_review.sh` script validates the positions output and sends a Telegram alert if the MCP call
fails, so token expiry won't go unnoticed.

---

## Enabling live execution

When you're ready to enable automated selling:

```bash
# In .env on the server:
LIVE_TRADING=true
```

With this set, any position at 0–1 engine conditions will be sold automatically at the 9:45 AM and
3:45 PM runs. Review output and actions.json are still written to `reviews/` for every run.

To re-enable dry run without editing .env:

```bash
./scripts/run_review.sh morning --dry-run
```

---

## Logs and output

| Path | Contents |
|------|----------|
| `reviews/YYYY-MM-DD_HHMM_session.txt` | Full review output for each run |
| `positions.json` | Last fetched portfolio snapshot |
| `actions.json` | Last generated structured actions |
| `logs/cron.log` | Cron-level output (start/stop/errors) |

---

## AWS-specific notes

If running on EC2, a `t3.small` instance is sufficient. The pipeline makes two Claude API calls and
several yfinance requests per run — total runtime is typically under 60 seconds.

Make sure the instance has outbound HTTPS access to:
- `api.anthropic.com` (Claude API)
- `agent.robinhood.com` (Robinhood MCP)
- `query1.finance.yahoo.com` (yfinance)

If the instance is in a VPC with a restrictive security group, add outbound rules for port 443 to
these domains.

---

## Troubleshooting

**`claude -p` hangs or returns auth error**
Re-run the interactive auth step (Step 5). The Robinhood OAuth token has expired.

**`positions.json` is empty or malformed**
The MCP returned an error. Run `claude mcp list` to check the connection status.

**Cron job not running**
Check `logs/cron.log` and `sudo grep CRON /var/log/syslog` (Ubuntu) or `sudo journalctl -u crond` (Amazon Linux).

**`TZ=America/New_York` not supported by your cron**
Some older cron implementations ignore the TZ variable. Workaround: set `TZ=America/New_York` in your
shell profile on the server, or convert the times to UTC manually (accounting for DST):
- EDT (Mar–Nov): 9:45 AM ET = 13:45 UTC, 3:45 PM ET = 19:45 UTC
- EST (Nov–Mar): 9:45 AM ET = 14:45 UTC, 3:45 PM ET = 20:45 UTC
