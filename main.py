from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import stripe
import os
import httpx
from datetime import datetime, timezone

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


def plan_payload_from_subscription(sub):
    """
    IMPORTANT:
    If user cancels in Stripe portal, Stripe usually sets:
    - status = active
    - cancel_at_period_end = true

    You said website should stop showing PRO immediately.
    So here we downgrade immediately when cancel_at_period_end = true.
    """
    status = getattr(sub, "status", None) or sub.get("status")
    cancel_at_period_end = getattr(sub, "cancel_at_period_end", None)
    if cancel_at_period_end is None and isinstance(sub, dict):
        cancel_at_period_end = sub.get("cancel_at_period_end", False)

    current_period_end = getattr(sub, "current_period_end", None)
    if current_period_end is None and isinstance(sub, dict):
        current_period_end = sub.get("current_period_end")

    subscription_id = getattr(sub, "id", None)
    if subscription_id is None and isinstance(sub, dict):
        subscription_id = sub.get("id")

    customer_id = getattr(sub, "customer", None)
    if customer_id is None and isinstance(sub, dict):
        customer_id = sub.get("customer")

    if status in ("active", "trialing") and not cancel_at_period_end:
        return {
            "plan": "pro",
            "max_file_size_mb": 50,
            "daily_conversion_limit": 50,
            "subscription_status": status,
            "subscription_cancel_at_period_end": False,
            "current_period_end": to_iso(current_period_end),
            "stripe_customer_id": customer_id,
            "stripe_subscription_id": subscription_id,
        }

    return {
        "plan": "free",
        "max_file_size_mb": 5,
        "daily_conversion_limit": 2,
        "subscription_status": status or "canceled",
        "subscription_cancel_at_period_end": bool(cancel_at_period_end),
        "current_period_end": to_iso(current_period_end),
        "stripe_customer_id": customer_id,
        "stripe_subscription_id": None if status in ("canceled", "unpaid", "incomplete_expired") else subscription_id,
    }


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

        payload = plan_payload_from_subscription(sub)
        payload["stripe_customer_id"] = customer_id or payload.get("stripe_customer_id")
        payload["stripe_subscription_id"] = sub_id if payload["plan"] == "pro" else payload.get("stripe_subscription_id")

        await supabase_patch(
            "user_profiles",
            {"id": f"eq.{user_id}"},
            payload,
        )

        return {"success": True}

    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/create-billing-portal")
async def create_billing_portal(request: Request):
    user = await get_user(request)

    profile = await supabase_get(
        "user_profiles",
        {"id": f"eq.{user['id']}", "select": "stripe_customer_id"},
    )

    if not profile:
        raise HTTPException(status_code=404, detail="User profile not found")

    stripe_customer_id = profile[0].get("stripe_customer_id")
    if not stripe_customer_id:
        raise HTTPException(status_code=400, detail="No Stripe customer found")

    session = stripe.billing_portal.Session.create(
        customer=stripe_customer_id,
        return_url=f"{APP_BASE_URL}/dashboard.html",
    )

    return {"url": session.url}


@app.post("/refresh-subscription")
async def refresh_subscription(request: Request):
    user = await get_user(request)

    profile = await supabase_get(
        "user_profiles",
        {"id": f"eq.{user['id']}", "select": "stripe_subscription_id,stripe_customer_id"},
    )

    if not profile:
        raise HTTPException(status_code=404, detail="User profile not found")

    row = profile[0]
    sub_id = row.get("stripe_subscription_id")
    customer_id = row.get("stripe_customer_id")

    if not sub_id:
        payload = {
            "plan": "free",
            "max_file_size_mb": 5,
            "daily_conversion_limit": 2,
            "subscription_status": "inactive",
            "subscription_cancel_at_period_end": False,
            "current_period_end": None,
        }
        await supabase_patch("user_profiles", {"id": f"eq.{user['id']}"}, payload)
        return {"ok": True, **payload}

    sub = stripe.Subscription.retrieve(sub_id)
    payload = plan_payload_from_subscription(sub)
    payload["stripe_customer_id"] = customer_id or payload.get("stripe_customer_id")
    await supabase_patch("user_profiles", {"id": f"eq.{user['id']}"}, payload)

    return {"ok": True, **payload}


@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

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
                    "subscription_cancel_at_period_end": False,
                    "stripe_customer_id": getattr(obj, "customer", None),
                    "stripe_subscription_id": getattr(obj, "subscription", None),
                },
            )

    elif event["type"] in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        sub_id = getattr(obj, "id", None)
        customer_id = getattr(obj, "customer", None)

        rows = await supabase_get(
            "user_profiles",
            {"stripe_customer_id": f"eq.{customer_id}", "select": "id"},
        )

        if not rows and sub_id:
            rows = await supabase_get(
                "user_profiles",
                {"stripe_subscription_id": f"eq.{sub_id}", "select": "id"},
            )

        if rows:
            user_id = rows[0]["id"]
            payload = plan_payload_from_subscription(obj)
            payload["stripe_customer_id"] = customer_id or payload.get("stripe_customer_id")
            await supabase_patch(
                "user_profiles",
                {"id": f"eq.{user_id}"},
                payload,
            )

    elif event["type"] == "invoice.payment_failed":
        customer_id = getattr(obj, "customer", None)

        rows = await supabase_get(
            "user_profiles",
            {"stripe_customer_id": f"eq.{customer_id}", "select": "id"},
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
                    "subscription_status": "past_due",
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
        return {
            "plan": "free",
            "max_file_size_mb": 5,
            "daily_conversion_limit": 2,
            "subscription_status": "inactive",
            "subscription_cancel_at_period_end": False,
            "current_period_end": None,
        }

    return profile[0]
