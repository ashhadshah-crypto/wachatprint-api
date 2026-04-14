from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import stripe
import os
from supabase import create_client, Client

app = FastAPI()

# CORS (adjust if needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ENV VARIABLES
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID_PRO_MONTHLY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# INIT
stripe.api_key = STRIPE_SECRET_KEY
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ----------------------------------------
# CREATE CHECKOUT SESSION
# ----------------------------------------
@app.post("/create-checkout-session")
async def create_checkout_session(request: Request):
    body = await request.json()

    user_id = body.get("user_id")
    email = body.get("email")

    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user_id")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            customer_email=email,
            line_items=[{
                "price": STRIPE_PRICE_ID,
                "quantity": 1
            }],
            success_url="https://www.wachatprint.com/billing-success.html?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://www.wachatprint.com/billing-cancel.html",
            client_reference_id=user_id,
            metadata={
                "user_id": user_id,
                "email": email
            }
        )

        return {"url": session.url}

    except Exception as e:
        print("CHECKOUT ERROR:", str(e))
        raise HTTPException(status_code=500, detail=str(e))


# ----------------------------------------
# VERIFY CHECKOUT SESSION (FIX)
# ----------------------------------------
@app.post("/verify-checkout-session")
async def verify_checkout_session(request: Request):
    body = await request.json()
    session_id = body.get("session_id")

    if not session_id:
        return {"success": False, "error": "No session id"}

    try:
        session = stripe.checkout.Session.retrieve(session_id)

        if session.payment_status != "paid":
            return {"success": False, "error": "Not paid"}

        user_id = session.client_reference_id
        customer_id = session.customer
        subscription_id = session.subscription

        # UPDATE USER PROFILE
        supabase.table("user_profiles").update({
            "plan": "pro",
            "subscription_status": "active",
            "stripe_customer_id": customer_id,
            "stripe_subscription_id": subscription_id,
            "max_file_size_mb": 50,
            "daily_conversion_limit": 50
        }).eq("id", user_id).execute()

        return {"success": True}

    except Exception as e:
        print("VERIFY ERROR:", str(e))
        return {"success": False, "error": str(e)}


# ----------------------------------------
# STRIPE WEBHOOK (BACKUP)
# ----------------------------------------
@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        print("Webhook signature error:", str(e))
        return {"status": "error"}

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]

        user_id = session.get("client_reference_id")
        customer_id = session.get("customer")
        subscription_id = session.get("subscription")

        try:
            supabase.table("user_profiles").update({
                "plan": "pro",
                "subscription_status": "active",
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": subscription_id,
                "max_file_size_mb": 50,
                "daily_conversion_limit": 50
            }).eq("id", user_id).execute()

        except Exception as e:
            print("Webhook DB error:", str(e))

    return {"status": "success"}
