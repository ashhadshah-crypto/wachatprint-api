# main_fixed.py

# Replace only the relevant parts in your existing main.py

# ---- FIXED VERIFY CHECKOUT SESSION ----
@app.post("/verify-checkout-session")
async def verify_checkout_session(request: Request):
    try:
        user = await get_authenticated_user(request)
        body = await request.json()
        session_id = body.get("session_id")

        if not session_id:
            raise HTTPException(status_code=400, detail="Missing session_id.")

        session = await asyncio.to_thread(
            stripe.checkout.Session.retrieve,
            session_id,
        )

        session_user_id = getattr(session, "client_reference_id", None)
        if not session_user_id:
            metadata = getattr(session, "metadata", {}) or {}
            session_user_id = metadata.get("user_id")

        if session_user_id != user["id"]:
            raise HTTPException(status_code=403, detail="Session mismatch")

        payment_status = getattr(session, "payment_status", None)
        customer_id = getattr(session, "customer", None)
        subscription_id = getattr(session, "subscription", None)

        if payment_status != "paid":
            return {"success": False, "message": "Not paid yet"}

        subscription_status = "active"
        current_period_end = None

        if subscription_id:
            subscription = await asyncio.to_thread(
                stripe.Subscription.retrieve,
                subscription_id
            )
            subscription_status = getattr(subscription, "status", "active")
            current_period_end = to_iso_from_unix(
                getattr(subscription, "current_period_end", None)
            )

        await supabase_patch(
            "user_profiles",
            {"id": f"eq.{user['id']}"},
            pro_profile_payload(
                subscription_status,
                customer_id,
                subscription_id,
                current_period_end,
            ),
        )

        return {"success": True}

    except Exception as e:
        return {"success": False, "error": str(e)}


# ---- FIXED STRIPE WEBHOOK ----
@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    event = await asyncio.to_thread(
        stripe.Webhook.construct_event,
        payload,
        sig_header,
        STRIPE_WEBHOOK_SECRET,
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

    return {"received": True}
