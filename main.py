from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import stripe
import os
import httpx
from datetime import datetime, timezone, timedelta

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID_PRO_MONTHLY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

APP_BASE_URL = os.getenv("APP_BASE_URL", "https://www.wachatprint.com")

stripe.api_key = STRIPE_SECRET_KEY


def to_iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


async def supabase_patch(table, filters, payload):
    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.patch(
            f"{SUPABASE_URL}/rest/v1/{table}",
            params=filters,
            json=payload,
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )
    if res.status_code not in (200, 204):
        raise HTTPException(status_code=500, detail=res.text)


async def supabase_get(table, params):
    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            params=params,
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            },
        )
    if res.status_code != 200:
        raise HTTPException(status_code=500, detail=res.text)
    return res.json()


async def get_user(request: Request):
    token = request.headers.get("authorization", "").replace("Bearer ", "").strip()

    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {token}",
            },
        )

    if res.status_code != 200:
        raise HTTPException(status_code=401, detail="Unauthorized")

    return res.json()


@app.get("/")
async def root():
    return {"ok": True}


@app.post("/create-checkout-session")
async def create_checkout_session(request: Request):
    user = await get_user(request)

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="subscription",
        customer_email=user.get("email"),
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=f"{APP_BASE_URL}/billing-success.html?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{APP_BASE_URL}/billing-cancel.html",
        client_reference_id=user["id"],
    )

    return {"url": session.url}


@app.post("/verify-checkout-session")
async def verify_checkout_session(request: Request):
    try:
        user = await get_user(request)
        body = await request.json()
        session_id = body.get("session_id")

        session = stripe.checkout.Session.retrieve(session_id)

        user_id = getattr(session, "client_reference_id", None)

        if user_id != user["id"]:
            raise HTTPException(403, "Mismatch")

        if getattr(session, "payment_status", None) != "paid":
            return {"success": False}

        sub_id = getattr(session, "subscription", None)
        customer_id = getattr(session, "customer", None)

        sub = stripe.Subscription.retrieve(sub_id)

        await supabase_patch(
            "user_profiles",
            {"id": f"eq.{user_id}"},
            {
                "plan": "pro",
                "max_file_size_mb": 50,
                "daily_conversion_limit": 50,
                "subscription_status": getattr(sub, "status", "active"),
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": sub_id,
                "current_period_end": to_iso(getattr(sub, "current_period_end", None)),
            },
        )

        return {"success": True}

    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")

    event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    obj = event["data"]["object"]

    if event["type"] == "checkout.session.completed":
        user_id = getattr(obj, "client_reference_id", None)
        if user_id:
            await supabase_patch(
                "user_profiles",
                {"id": f"eq.{user_id}"},
                {
                    "plan": "pro",
                    "max_file_size_mb": 50,
                    "daily_conversion_limit": 50,
                    "subscription_status": "active",
                    "stripe_customer_id": getattr(obj, "customer", None),
                    "stripe_subscription_id": getattr(obj, "subscription", None),
                },
            )

    elif event["type"] == "customer.subscription.deleted":
        sub_id = getattr(obj, "id", None)
        customer_id = getattr(obj, "customer", None)

        rows = await supabase_get(
            "user_profiles",
            {"stripe_subscription_id": f"eq.{sub_id}", "select": "id"},
        )

        if rows:
            user_id = rows[0]["id"]
            await supabase_patch(
                "user_profiles",
                {"id": f"eq.{user_id}"},
                {
                    "plan": "free",
                    "max_file_size_mb": 5,
                    "daily_conversion_limit": 2,
                    "subscription_status": "canceled",
                    "stripe_customer_id": customer_id,
                    "stripe_subscription_id": None,
                },
            )

    return {"ok": True}


@app.get("/usage-summary")
async def usage_summary(request: Request):
    user = await get_user(request)

    profile = await supabase_get(
        "user_profiles",
        {"id": f"eq.{user['id']}", "select": "*"},
    )

    if not profile:
        return {"plan": "free", "max_file_size_mb": 5, "daily_conversion_limit": 2}

    return profile[0]
