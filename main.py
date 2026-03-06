from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import lighter

BASE_URL = "https://mainnet.zklighter.elliot.ai"

app = FastAPI(title="Lighter Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AccountRequest(BaseModel):
    l1_address: str


class TradesRequest(BaseModel):
    private_key: str
    api_key_index: int
    account_index: int
    limit: Optional[int] = 100
    cursor: Optional[str] = None
    sort_dir: Optional[str] = "desc"
    market_id: Optional[int] = None


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/account")
async def get_account(req: AccountRequest):
    """
    Resolve L1 Ethereum address → sub-accounts (no auth required).
    Uses lighter SDK: AccountApi.accounts_by_l1_address
    """
    try:
        client = lighter.ApiClient(lighter.Configuration(host=BASE_URL))
        account_api = lighter.AccountApi(client)
        resp = await account_api.accounts_by_l1_address(l1_address=req.l1_address)
        await client.close()

        if not resp or not resp.sub_accounts:
            raise HTTPException(status_code=404, detail="No account found for this address")

        return {
            "l1_address": resp.l1_address,
            "sub_accounts": [
                {
                    "index": a.index,
                    "l1_address": getattr(a, "l1_address", None),
                    "status": getattr(a, "status", None),
                    "available_balance": getattr(a, "available_balance", None),
                    "collateral": getattr(a, "collateral", None),
                }
                for a in resp.sub_accounts
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/trades")
async def get_trades(req: TradesRequest):
    """
    Fetch trade history for a Lighter account.
    Uses lighter SDK: SignerClient → create_auth_token_with_expiry → OrderApi.trades
    Private key used only to generate a short-lived auth token, never stored.
    """
    try:
        # Initialise SignerClient with the user's read-only API key
        signer = lighter.SignerClient(
            url=BASE_URL,
            api_private_keys={req.api_key_index: req.private_key},
            account_index=req.account_index,
        )

        # Generate short-lived auth token (10 minutes)
        auth_token, err = signer.create_auth_token_with_expiry(deadline=600)
        if err:
            raise HTTPException(status_code=401, detail=f"Auth token generation failed: {err}")

        # Fetch trades using the SDK OrderApi — pass auth token, filter to this account
        order_api = lighter.OrderApi(signer)
        result: lighter.Trades = await order_api.trades(
            sort_by="timestamp",
            limit=min(req.limit or 100, 100),
            authorization=auth_token,
            account_index=req.account_index,
            sort_dir=req.sort_dir or "desc",
            cursor=req.cursor or None,
            market_id=req.market_id,
        )
        await signer.close()

        if not result or not result.trades:
            return {"trades": [], "next_cursor": None}

        # Enrich each trade: determine user side + correct PnL field
        account_idx = req.account_index
        enriched: List[Dict[str, Any]] = []

        for t in result.trades:
            is_ask_side = (t.ask_account_id == account_idx)
            is_bid_side = (t.bid_account_id == account_idx)
            user_side   = "sell" if is_ask_side else "buy" if is_bid_side else "unknown"
            pnl         = float(t.ask_account_pnl or 0) if is_ask_side else float(t.bid_account_pnl or 0)
            fee         = float(t.taker_fee or 0) if not (t.is_maker_ask and is_ask_side) else float(t.maker_fee or 0)

            enriched.append({
                "trade_id":    t.trade_id,
                "tx_hash":     t.tx_hash,
                "market_id":   t.market_id,
                "side":        user_side,
                "is_ask":      is_ask_side,
                "price":       float(t.price or 0),
                "size":        float(t.size or 0),
                "usd_amount":  float(t.usd_amount or 0),
                "pnl":         pnl,
                "fee":         fee,
                "timestamp":   t.timestamp,
                "block_height": t.block_height,
                "type":        t.type,
                "taker_position_sign_changed": t.taker_position_sign_changed,
                "maker_position_sign_changed": t.maker_position_sign_changed,
            })

        return {
            "trades":      enriched,
            "next_cursor": result.next_cursor,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
