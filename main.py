import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

BASE_URL = "https://mainnet.zklighter.elliot.ai/api/v1"

app = FastAPI(title="Lighter Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class BalanceRequest(BaseModel):
    read_only_token: str


class TradesRequest(BaseModel):
    read_only_token: str
    limit: Optional[int] = 100
    cursor: Optional[str] = None
    sort_dir: Optional[str] = "desc"
    market_id: Optional[int] = None


def parse_account_index(token: str) -> int:
    """Extract account_index from ro:<account_index>:<scope>:<expiry>:<sig>"""
    try:
        parts = token.split(":")
        return int(parts[1])
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid read-only token format")


def lighter_headers(token: str) -> Dict[str, str]:
    return {"Authorization": token}


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/balance")
async def get_balance(req: BalanceRequest):
    """
    Fetch Lighter account balance using a read-only token.
    Extracts account_index from token, calls /account endpoint.
    """
    account_index = parse_account_index(req.read_only_token)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/account",
            params={"by": "index", "value": str(account_index)},
            headers=lighter_headers(req.read_only_token),
            timeout=10.0,
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=f"Lighter API error: {resp.text[:200]}")
    data = resp.json()
    if data.get("code") != 200:
        raise HTTPException(status_code=400, detail=data.get("message", "Unknown error"))

    accounts = data.get("accounts", [])
    if not accounts:
        raise HTTPException(status_code=404, detail="Account not found")

    a = accounts[0]
    return {
        "account_index":    a.get("account_index", account_index),
        "l1_address":       a.get("l1_address"),
        "account_type":     a.get("account_type"),  # 0=trading, 1=public pool LP
        "available_balance": float(a.get("available_balance") or 0),
        "collateral":       float(a.get("collateral") or 0),
        "total_asset_value": float(a.get("total_asset_value") or 0),
        "positions":        a.get("positions", []),
        "shares":           a.get("shares", []),
    }


@app.post("/trades")
async def get_trades(req: TradesRequest):
    """
    Fetch trade history for a Lighter account using a read-only token.
    Uses the lighter-sdk OrderApi with the token passed as Authorization header.
    """
    account_index = parse_account_index(req.read_only_token)

    import lighter

    # Build ApiClient configured with the read-only token as default Authorization header
    config = lighter.Configuration(host="https://mainnet.zklighter.elliot.ai")
    client = lighter.ApiClient(config)
    client.default_headers["Authorization"] = req.read_only_token

    try:
        order_api = lighter.OrderApi(client)
        result: lighter.Trades = await order_api.trades(
            sort_by="timestamp",
            limit=min(req.limit or 100, 100),
            authorization=req.read_only_token,
            account_index=account_index,
            sort_dir=req.sort_dir or "desc",
            cursor=req.cursor or None,
            market_id=req.market_id,
        )
        await client.close()
    except Exception as e:
        await client.close()
        raise HTTPException(status_code=500, detail=str(e))

    if not result or not result.trades:
        return {"trades": [], "next_cursor": None}

    enriched: List[Dict[str, Any]] = []
    for t in result.trades:
        is_ask_side = (t.ask_account_id == account_index)
        user_side   = "sell" if is_ask_side else "buy"
        pnl         = float(t.ask_account_pnl or 0) if is_ask_side else float(t.bid_account_pnl or 0)
        fee         = float(t.taker_fee or 0) if not (t.is_maker_ask and is_ask_side) else float(t.maker_fee or 0)

        enriched.append({
            "trade_id":   t.trade_id,
            "tx_hash":    t.tx_hash,
            "market_id":  t.market_id,
            "side":       user_side,
            "is_ask":     is_ask_side,
            "price":      float(t.price or 0),
            "size":       float(t.size or 0),
            "usd_amount": float(t.usd_amount or 0),
            "pnl":        pnl,
            "fee":        fee,
            "timestamp":  t.timestamp,
            "type":       t.type,
        })

    return {"trades": enriched, "next_cursor": result.next_cursor}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
