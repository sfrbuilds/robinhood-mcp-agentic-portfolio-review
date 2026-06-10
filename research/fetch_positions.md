Use the Robinhood Trading MCP to fetch my current portfolio.

Call these tools in order:
1. get_account — to get total equity, buying power, and cash balance
2. list_positions — to get all current equity positions

Then output ONLY a single valid JSON object in exactly this format (no other text, no markdown, no explanation):

{
  "account": {
    "equity": <float>,
    "cash": <float>,
    "buying_power": <float>
  },
  "positions": [
    {
      "symbol": "<string>",
      "quantity": <float>,
      "avg_cost": <float>,
      "current_price": <float>,
      "current_value": <float>,
      "unrealized_pl": <float>,
      "unrealized_pl_pct": <float>
    }
  ]
}

If a field is not available, use null. Output nothing except the JSON object.
