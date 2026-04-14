from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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
        raise HTTPException(status_code=500, detail=f"Supabase patch failed: {res.text}")


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
        raise HTTPException(status_code=500, detail=f"Supabase get failed: {res.text}")
    return res.json()


async def supabase_insert(table, payload):
    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            json=payload,
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )
    if res.status_code not in (200, 201, 204):
        raise HTTPException(status_code=500, detail=f"Supabase insert failed: {res.text}")


async def get_user(request: Request):
    auth_header = request.headers.get("authorization", "")
    token = auth_header.replace("Bearer ", "").strip()

    if not token:
        raise HTTPException(status_code=401, detail="Missing auth token")

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
    return {"status": "ok", "app": "WAChatPrint API"}


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
        success_url=f"{APP_BASE_URL}/billing-success.html?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{APP_BASE_URL}/billing-cancel.html",
        client_reference_id=user["id"],
        metadata={
            "user_id": user["id"],
            "email": user.get("email", "")
        }
    )

    return {"url": session.url}


@app.post("/verify-checkout-session")
async def verify_checkout_session(request: Request):
    try:
        user = await get_user(request)
        body = await request.json()
        session_id = body.get("session_id")

        if not session_id:
            raise HTTPException(status_code=400, detail="Missing session_id")

        session = stripe.checkout.Session.retrieve(session_id)

        user_id = getattr(session, "client_reference_id", None)
        if not user_id:
            metadata = getattr(session, "metadata", {}) or {}
            user_id = metadata.get("user_id")

        if user_id != user["id"]:
            raise HTTPException(status_code=403, detail="User mismatch")

        payment_status = getattr(session, "payment_status", None)
        customer_id = getattr(session, "customer", None)
        subscription_id = getattr(session, "subscription", None)

        if payment_status != "paid":
            return {"success": False, "message": "Payment not marked paid yet"}

        subscription_status = "active"
        current_period_end = None

        if subscription_id:
            sub = stripe.Subscription.retrieve(subscription_id)
            subscription_status = getattr(sub, "status", "active")
            current_period_end = to_iso(getattr(sub, "current_period_end", None))

        await supabase_patch(
            "user_profiles",
            {"id": f"eq.{user_id}"},
            {
                "plan": "pro",
                "max_file_size_mb": 50,
                "daily_conversion_limit": 50,
                "subscription_status": subscription_status,
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": subscription_id,
                "current_period_end": current_period_end,
            },
        )

        return {"success": True}

    except HTTPException:
        raise
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}

    obj = event["data"]["object"]

    if event["type"] == "checkout.session.completed":
        user_id = getattr(obj, "client_reference_id", None)
        if not user_id:
            metadata = getattr(obj, "metadata", {}) or {}
            user_id = metadata.get("user_id")

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

    elif event["type"] == "customer.subscription.updated":
        sub_id = getattr(obj, "id", None)
        customer_id = getattr(obj, "customer", None)
        status = getattr(obj, "status", None)
        period_end = to_iso(getattr(obj, "current_period_end", None))

        rows = await supabase_get(
            "user_profiles",
            {
                "stripe_subscription_id": f"eq.{sub_id}",
                "select": "id"
            }
        )

        if not rows and customer_id:
            rows = await supabase_get(
                "user_profiles",
                {
                    "stripe_customer_id": f"eq.{customer_id}",
                    "select": "id"
                }
            )

        if rows:
            user_id = rows[0]["id"]

            if status in ("active", "trialing", "past_due"):
                await supabase_patch(
                    "user_profiles",
                    {"id": f"eq.{user_id}"},
                    {
                        "plan": "pro",
                        "max_file_size_mb": 50,
                        "daily_conversion_limit": 50,
                        "subscription_status": status,
                        "stripe_customer_id": customer_id,
                        "stripe_subscription_id": sub_id,
                        "current_period_end": period_end,
                    },
                )
            else:
                await supabase_patch(
                    "user_profiles",
                    {"id": f"eq.{user_id}"},
                    {
                        "plan": "free",
                        "max_file_size_mb": 5,
                        "daily_conversion_limit": 2,
                        "subscription_status": status or "inactive",
                        "stripe_customer_id": customer_id,
                        "stripe_subscription_id": None,
                        "current_period_end": period_end,
                    },
                )

    elif event["type"] == "customer.subscription.deleted":
        sub_id = getattr(obj, "id", None)
        customer_id = getattr(obj, "customer", None)
        period_end = to_iso(getattr(obj, "current_period_end", None))

        rows = await supabase_get(
            "user_profiles",
            {
                "stripe_subscription_id": f"eq.{sub_id}",
                "select": "id"
            }
        )

        if not rows and customer_id:
            rows = await supabase_get(
                "user_profiles",
                {
                    "stripe_customer_id": f"eq.{customer_id}",
                    "select": "id"
                }
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
                    "current_period_end": period_end,
                },
            )

    return {"ok": True}


@app.post("/create-portal-session")
async def create_portal_session(request: Request):
    user = await get_user(request)

    rows = await supabase_get(
        "user_profiles",
        {
            "id": f"eq.{user['id']}",
            "select": "stripe_customer_id"
        }
    )

    if not rows or not rows[0].get("stripe_customer_id"):
        raise HTTPException(status_code=400, detail="No billing profile found")

    customer_id = rows[0]["stripe_customer_id"]

    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=f"{APP_BASE_URL}/dashboard.html",
    )

    return {"url": session.url}


@app.get("/usage-summary")
async def usage_summary(request: Request):
    user = await get_user(request)

    profile_rows = await supabase_get(
        "user_profiles",
        {
            "id": f"eq.{user['id']}",
            "select": "*"
        }
    )

    if not profile_rows:
        return {
            "plan": "free",
            "max_file_size_mb": 5,
            "daily_conversion_limit": 2,
            "used_last_24h": 0,
            "remaining_today": 2,
            "subscription_status": "inactive",
            "current_period_end": None,
        }

    profile = profile_rows[0]

    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    usage_rows = await supabase_get(
        "conversion_usage",
        {
            "user_id": f"eq.{user['id']}",
            "created_at": f"gte.{since}",
            "select": "id"
        }
    )

    used = len(usage_rows or [])
    daily_limit = int(profile.get("daily_conversion_limit") or 2)

    return {
        "plan": profile.get("plan", "free"),
        "max_file_size_mb": int(profile.get("max_file_size_mb") or 5),
        "daily_conversion_limit": daily_limit,
        "used_last_24h": used,
        "remaining_today": max(daily_limit - used, 0),
        "subscription_status": profile.get("subscription_status") or "inactive",
        "current_period_end": profile.get("current_period_end"),
    }


@app.post("/record-usage")
async def record_usage(request: Request):
    user = await get_user(request)

    await supabase_insert(
        "conversion_usage",
        {
            "user_id": user["id"]
        }
    )

    return {"ok": True}
