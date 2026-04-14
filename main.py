from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import stripe
import os
import httpx
import asyncio
from datetime import datetime, timezone

app = FastAPI()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ENV
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID_PRO_MONTHLY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

stripe.api_key = STRIPE_SECRET_KEY

# -----------------------------
# SUPABASE HELPERS (REST)
# -----------------------------
async def supabase_patch(table, filters, payload):
    async with httpx.AsyncClient() as client:
        await client.patch(
            f"{SUPABASE_URL}/rest/v1/{table}",
            params=filters,
            json=payload,
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
            },
        )

async def get_user(request: Request):
    token = request.headers.get("authorization", "").replace("Bearer ", "")

    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "apikey": os.getenv("SUPABASE_ANON_KEY"),
                "Authorization": f"Bearer {token}",
            },
        )

    if res.status_code != 200:
        raise HTTPException(401, "Unauthorized")

    return res.json()

def to_iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

# -----------------------------
# CHECKOUT SESSION
# -----------------------------
@app.post("/create-checkout-session")
async def create_checkout_session(request: Request):
    user = await get_user(request)

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="subscription",
        customer_email=user.get("email"),
        line_items=[{
            "price": STRIPE_PRICE_ID,
            "quantity": 1
        }],
        success_url="https://www.wachatprint.com/billing-success.html?session_id={CHECKOUT_SESSION_ID}",
        cancel_url="https://www.wachatprint.com/billing-cancel.html",
        client_reference_id=user["id"],
    )

    return {"url": session.url}

# -----------------------------
# VERIFY CHECKOUT (FIX)
# -----------------------------
@app.post("/verify-checkout-session")
async def verify_checkout_session(request: Request):
    try:
        user = await get_user(request)
        body = await request.json()
        session_id = body.get("session_id")

        if not session_id:
            raise HTTPException(400, "Missing session_id")

        session = stripe.checkout.Session.retrieve(session_id)

        user_id = getattr(session, "client_reference_id", None)

        if user_id != user["id"]:
            raise HTTPException(403, "User mismatch")

        payment_status = getattr(session, "payment_status", None)
        customer_id = getattr(session, "customer", None)
        subscription_id = getattr(session, "subscription", None)

        if payment_status != "paid":
            return {"success": False}

        sub = stripe.Subscription.retrieve(subscription_id)

        await supabase_patch(
            "user_profiles",
            {"id": f"eq.{user_id}"},
            {
                "plan": "pro",
                "subscription_status": getattr(sub, "status", "active"),
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": subscription_id,
                "current_period_end": to_iso(getattr(sub, "current_period_end", None)),
            },
        )

        return {"success": True}

    except Exception as e:
        return {"success": False, "error": str(e)}

# -----------------------------
# WEBHOOK (BACKUP)
# -----------------------------
@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")

    event = stripe.Webhook.construct_event(
        payload, sig, STRIPE_WEBHOOK_SECRET
    )

    obj = event["data"]["object"]

    if event["type"] == "checkout.session.completed":
        user_id = getattr(obj, "client_reference_id", None)

        if user_id:
            await supabase_patch(
                "user_profiles",
                {"id": f"eq.{user_id}"},
                {
                    "plan": "pro",
                    "subscription_status": "active",
                    "stripe_customer_id": getattr(obj, "customer", None),
                    "stripe_subscription_id": getattr(obj, "subscription", None),
                },
            )

    return {"ok": True}

# -----------------------------
# USAGE SUMMARY (FOR DASHBOARD)
# -----------------------------
@app.get("/usage-summary")
async def usage_summary(request: Request):
    user = await get_user(request)

    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{SUPABASE_URL}/rest/v1/user_profiles",
            params={
                "id": f"eq.{user['id']}",
                "select": "*"
            },
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            },
        )

    data = res.json()

    if not data:
        return {"plan": "free"}

    return data[0]
