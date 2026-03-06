from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import lighter
import asyncio

BASE_URL = "https://mainnet.zklighter.elliot.ai"

app = FastAPI(title="Lighter Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class TradesRequest(BaseModel):
    private_key: str
    api_key_index: int
    account_index: int
    limit: Optional[int] = 100
    cursor: Optional[str] = None
    sort_dir: Optional[str] = "desc"

class AccountRequest(BaseModel):
    l1_address: str

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/account")
async def get_account(req: AccountRequest):
    """Resolve L1 address → account index (no auth needed)"""
    try:
        client = lighter.ApiClient(lighter.Configuration(host=BASE_URL))
        account_api = lighter.AccountApi(client)
        resp = await account_api.accounts_by_l1_address(l1_address=req.l1_address)
        await client.close()
        return {
            "sub_accounts": [
                {"index": a.index, "public_key": getattr(a, "public_key", None)}
                for a in (resp.sub_accounts or [])
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/trades")
async def get_trades(req: TradesRequest):
    """Fetch trade history for account — private key used only for auth token, never stored"""
    try:
        signer_client = lighter.SignerClient(
            url=BASE_URL,
            api_private_keys={req.api_key_index: req.private_key},
            account_index=req.account_index,
        )

        auth_token, err = signer_client.create_auth_token_with_expiry(deadline=600)  # 10 min token
        if err:
            raise HTTPException(status_code=401, detail=f"Auth failed: {err}")

        order_api = lighter.OrderApi(signer_client)
        result = await order_api.trades(
            sort_by="timestamp",
            limit=min(req.limit or 100, 100),
            authorization=auth_token,
            account_index=req.account_index,
            sort_dir=req.sort_dir or "desc",
            cursor=req.cursor,
        )
        await signer_client.close()

        # Serialize to dict
        if hasattr(result, "model_dump"):
            return result.model_dump()
        elif hasattr(result, "__dict__"):
            return result.__dict__
        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
