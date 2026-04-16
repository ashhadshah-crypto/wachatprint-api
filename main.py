from io import BytesIO
import html
import os
import re
import zipfile
from typing import List, Dict
from urllib.parse import quote
from datetime import datetime, timedelta, timezone

import httpx
import stripe
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from playwright.async_api import async_playwright
from pypdf import PdfReader, PdfWriter


app = FastAPI(title="WAChatPrint API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://www.wachatprint.com",
        "https://wachatprint.com",
        "http://localhost:3000",
        "http://127.0.0.1:5500",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ABSOLUTE_MAX_FILE_SIZE = 50 * 1024 * 1024

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID_PRO_MONTHLY") or os.getenv("STRIPE_PRICE_ID_PRO")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

APP_BASE_URL = os.getenv("APP_BASE_URL", "https://www.wachatprint.com")

stripe.api_key = STRIPE_SECRET_KEY

PATTERN_BRACKET = re.compile(
    r"^\[(?P<date>[^,\]]+),\s*(?P<time>[^\]]+)\]\s*(?P<rest>.+)$"
)
PATTERN_DASH = re.compile(
    r"^(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{2,4}),\s*(?P<time>[^-]+?)\s*-\s*(?P<rest>.+)$"
)


@app.get("/")
def root():
    return {"message": "WAChatPrint API is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


def to_iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def split_sender_and_message(rest: str):
    if ": " in rest:
        sender, message = rest.split(": ", 1)
        return sender.strip(), message.strip(), False
    return None, rest.strip(), True


def parse_whatsapp_text(text: str) -> List[Dict]:
    messages: List[Dict] = []
    sender_side_map: Dict[str, str] = {}
    last_date = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        if not line.strip():
            if messages:
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("type") == "message":
                        messages[i]["message"] += "\n"
                        break
            continue

        match = PATTERN_BRACKET.match(line) or PATTERN_DASH.match(line)

        if match:
            date = match.group("date").strip()
            time = match.group("time").strip()
            rest = match.group("rest").strip()

            sender, message, is_system = split_sender_and_message(rest)

            if sender:
                if sender not in sender_side_map:
                    sender_side_map[sender] = "left" if len(sender_side_map) % 2 == 0 else "right"
                side = sender_side_map[sender]
            else:
                side = "center"

            if date != last_date:
                messages.append({"type": "date_separator", "date": date})
                last_date = date

            messages.append(
                {
                    "type": "message",
                    "date": date,
                    "time": time,
                    "sender": sender,
                    "message": message,
                    "is_system": is_system,
                    "side": side,
                }
            )
        else:
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("type") == "message":
                    if messages[i]["message"]:
                        messages[i]["message"] += "\n" + line
                    else:
                        messages[i]["message"] = line
                    break
            else:
                messages.append(
                    {
                        "type": "message",
                        "date": "",
                        "time": "",
                        "sender": None,
                        "message": line,
                        "is_system": True,
                        "side": "center",
                    }
                )

    return messages


def format_message_html(text: str) -> str:
    return html.escape(text).replace("\n", "<br>")


def build_download_header(filename: str) -> str:
    safe_ascii = "".join(
        ch if (32 <= ord(ch) < 127 and ch not in ['"', "\\"]) else "_"
        for ch in filename
    ).strip()
    if not safe_ascii:
        safe_ascii = "download.pdf"

    encoded_utf8 = quote(filename)
    return f"attachment; filename=\"{safe_ascii}\"; filename*=UTF-8''{encoded_utf8}"


def chunk_items(items: List[Dict], chunk_size: int = 80) -> List[List[Dict]]:
    chunks = []
    current = []
    for item in items:
        current.append(item)
        if len(current) >= chunk_size:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def decode_text_bytes(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return data.decode("latin-1", errors="ignore")


def choose_txt_from_zip(zip_file: zipfile.ZipFile) -> str:
    txt_files = [
        name for name in zip_file.namelist()
        if name.lower().endswith(".txt")
        and not name.endswith("/")
        and "__macosx" not in name.lower()
    ]
    if not txt_files:
        raise HTTPException(status_code=400, detail="No .txt chat file found inside ZIP.")

    def score(name: str):
        lower = name.lower()
        priority = 0
        if "whatsapp" in lower:
            priority -= 20
        if "chat" in lower:
            priority -= 10
        if lower.endswith("_chat.txt"):
            priority -= 10
        return (priority, len(name), name)

    txt_files.sort(key=score)
    return txt_files[0]


def extract_chat_text(filename: str, content: bytes):
    lower_name = (filename or "").lower()

    if lower_name.endswith(".txt"):
        source_name = os.path.splitext(os.path.basename(filename))[0]
        return source_name, decode_text_bytes(content)

    if lower_name.endswith(".zip"):
        try:
            with zipfile.ZipFile(BytesIO(content)) as zf:
                chosen_txt = choose_txt_from_zip(zf)
                txt_bytes = zf.read(chosen_txt)
                source_name = os.path.splitext(os.path.basename(chosen_txt))[0]
                return source_name, decode_text_bytes(txt_bytes)
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Invalid ZIP file.")

    raise HTTPException(status_code=400, detail="Only .txt and .zip files are supported.")


def build_chat_html(source_name: str, items: List[Dict], chunk_no: int, total_chunks: int) -> str:
    chunks: List[str] = []

    for item in items:
        if item["type"] == "date_separator":
            chunks.append(
                f"""
                <div class="date-separator-wrap">
                  <div class="date-separator">{html.escape(item["date"])}</div>
                </div>
                """
            )
            continue

        msg = item

        if msg["is_system"]:
            chunks.append(
                f"""
                <div class="system-wrap">
                  <div class="system-message">
                    <div class="system-text">{format_message_html(msg["message"])}</div>
                    <div class="system-time">{html.escape(msg["time"] or "")}</div>
                  </div>
                </div>
                """
            )
            continue

        side_class = "left" if msg["side"] == "left" else "right"
        sender_html = (
            f'<div class="sender">{html.escape(msg["sender"])}</div>'
            if msg["sender"]
            else ""
        )

        chunks.append(
            f"""
            <div class="msg-row {side_class}">
              <div class="bubble {side_class}">
                {sender_html}
                <div class="message-text">{format_message_html(msg["message"])}</div>
                <div class="meta">{html.escape(msg["time"] or "")}</div>
              </div>
            </div>
            """
        )

    chat_body = "\n".join(chunks)

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>{html.escape(source_name)}</title>
  <style>
    @page {{ size: A4; margin: 10mm 8mm 12mm 8mm; }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", "Segoe UI", Arial, sans-serif;
      background: #efeae2;
      color: #111827;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }}
    .topbar {{ background: #0f8f7d; color: white; padding: 14px 16px; font-weight: 700; font-size: 18px; border-radius: 10px; margin-bottom: 10px; }}
    .chat-card {{ background: #f6f1ea; border-radius: 14px; padding: 10px; }}
    .chat-title {{ background: white; border-radius: 12px; padding: 12px 14px; margin-bottom: 12px; display: flex; justify-content: space-between; gap: 12px; align-items: center; font-size: 13px; color: #374151; }}
    .chat-title strong {{ color: #111827; font-size: 14px; }}
    .date-separator-wrap, .system-wrap {{ text-align: center; margin: 10px 0; }}
    .date-separator {{ display: inline-block; background: #dbeafe; color: #1d4ed8; padding: 6px 12px; border-radius: 999px; font-size: 12px; font-weight: 600; }}
    .system-message {{ display: inline-block; max-width: 70%; background: #d1d5db; color: #374151; padding: 8px 12px; border-radius: 10px; font-size: 12px; line-height: 1.5; }}
    .system-time {{ margin-top: 4px; font-size: 10px; color: #6b7280; }}
    .msg-row {{ display: flex; margin: 7px 0; width: 100%; break-inside: avoid; page-break-inside: avoid; }}
    .msg-row.left {{ justify-content: flex-start; }}
    .msg-row.right {{ justify-content: flex-end; }}
    .bubble {{ max-width: 72%; padding: 8px 10px 6px; border-radius: 12px; line-height: 1.45; word-break: break-word; white-space: normal; break-inside: avoid; page-break-inside: avoid; }}
    .bubble.left {{ background: white; border-top-left-radius: 4px; }}
    .bubble.right {{ background: #dcf8c6; border-top-right-radius: 4px; }}
    .sender {{ font-size: 12px; font-weight: 700; margin-bottom: 4px; color: #2563eb; }}
    .bubble.right .sender {{ color: #0f8f7d; }}
    .message-text {{ font-size: 13px; color: #111827; }}
    .meta {{ margin-top: 4px; text-align: right; font-size: 10px; color: #6b7280; }}
  </style>
</head>
<body>
  <div class="topbar">WAChatPrint</div>
  <div class="chat-card">
    <div class="chat-title">
      <div><strong>{html.escape(source_name)}</strong></div>
      <div>Part {chunk_no} of {total_chunks}</div>
    </div>
    {chat_body}
  </div>
</body>
</html>
    """


async def render_chunk_pdf(browser, source_name: str, chunk: List[Dict], chunk_no: int, total_chunks: int) -> bytes:
    html_content = build_chat_html(source_name, chunk, chunk_no, total_chunks)
    page = await browser.new_page()
    try:
        await page.set_content(html_content, wait_until="load")
        await page.emulate_media(media="screen")
        return await page.pdf(
            format="A4",
            print_background=True,
            margin={"top": "10mm", "right": "8mm", "bottom": "12mm", "left": "8mm"},
            prefer_css_page_size=True,
        )
    finally:
        await page.close()


async def supabase_get(path: str, params=None, use_service_role: bool = True):
    key = SUPABASE_SERVICE_ROLE_KEY if use_service_role else SUPABASE_ANON_KEY
    async with httpx.AsyncClient(timeout=25) as client:
        res = await client.get(
            f"{SUPABASE_URL}{path}",
            params=params,
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
        )
    if res.status_code != 200:
        raise HTTPException(status_code=500, detail=res.text)
    return res.json()


async def supabase_post(path: str, payload: dict):
    async with httpx.AsyncClient(timeout=25) as client:
        res = await client.post(
            f"{SUPABASE_URL}{path}",
            json=payload,
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )
    if res.status_code not in (200, 201, 204):
        raise HTTPException(status_code=500, detail=res.text)


async def supabase_patch(table: str, filters: dict, payload: dict):
    async with httpx.AsyncClient(timeout=25) as client:
        res = await client.patch(
            f"{SUPABASE_URL}/rest/v1/{table}",
            params=filters,
            json=payload,
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )
    if res.status_code not in (200, 204):
        raise HTTPException(status_code=500, detail=res.text)


async def get_authenticated_user(request: Request):
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Login required.")

    access_token = auth_header.split(" ", 1)[1].strip()

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {access_token}"},
        )

    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid session. Please log in again.")
    return response.json()


async def get_or_create_user_profile(user_id: str, email: str = None):
    rows = await supabase_get("/rest/v1/user_profiles", {"id": f"eq.{user_id}", "select": "*"})
    if rows:
        profile = rows[0]
        patch = {}
        if profile.get("plan") is None:
            patch["plan"] = "free"
        if profile.get("max_file_size_mb") is None:
            patch["max_file_size_mb"] = 5
        if profile.get("daily_conversion_limit") is None:
            patch["daily_conversion_limit"] = 2
        if profile.get("subscription_status") is None:
            patch["subscription_status"] = "inactive"
        if email and not profile.get("email"):
            patch["email"] = email

        if patch:
            await supabase_patch("user_profiles", {"id": f"eq.{user_id}"}, patch)
            profile.update(patch)
        return profile

    payload = {
        "id": user_id,
        "email": email,
        "plan": "free",
        "max_file_size_mb": 5,
        "daily_conversion_limit": 2,
        "subscription_status": "inactive",
        "subscription_cancel_at_period_end": False,
    }
    await supabase_post("/rest/v1/user_profiles", payload)
    return payload


async def get_usage_count_last_24h(user_id: str) -> int:
    since = datetime.now(timezone.utc) - timedelta(days=1)
    rows = await supabase_get(
        "/rest/v1/conversion_usage",
        {"user_id": f"eq.{user_id}", "created_at": f"gte.{since.isoformat()}", "select": "id"},
    )
    return len(rows)


async def record_usage_event(user_id: str):
    await supabase_post("/rest/v1/conversion_usage", {"user_id": user_id})


def plan_payload_from_subscription(sub):
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


@app.post("/create-checkout-session")
async def create_checkout_session(request: Request):
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
        raise HTTPException(status_code=500, detail="Stripe config missing.")

    user = await get_authenticated_user(request)
    profile = await get_or_create_user_profile(user["id"], user.get("email"))

    customer_id = profile.get("stripe_customer_id")
    if not customer_id:
        customer = stripe.Customer.create(email=user.get("email"), metadata={"user_id": user["id"]})
        customer_id = customer.id
        await supabase_patch("user_profiles", {"id": f"eq.{user['id']}"}, {"stripe_customer_id": customer_id})

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="subscription",
        customer=customer_id,
        customer_email=None,
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=f"{APP_BASE_URL}/dashboard.html?billing=success&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{APP_BASE_URL}/dashboard.html?billing=cancel",
        client_reference_id=user["id"],
        allow_promotion_codes=True,
    )
    return {"url": session.url}


@app.post("/verify-checkout-session")
async def verify_checkout_session(request: Request):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe config missing.")

    user = await get_authenticated_user(request)
    body = await request.json()
    session_id = body.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    session = stripe.checkout.Session.retrieve(session_id)
    user_id = getattr(session, "client_reference_id", None)
    if user_id != user["id"]:
        raise HTTPException(status_code=403, detail="Mismatch")

    sub_id = getattr(session, "subscription", None)
    customer_id = getattr(session, "customer", None)
    if not sub_id:
        raise HTTPException(status_code=400, detail="Subscription not found")

    sub = stripe.Subscription.retrieve(sub_id)
    payload = plan_payload_from_subscription(sub)
    payload["stripe_customer_id"] = customer_id or payload.get("stripe_customer_id")
    payload["stripe_subscription_id"] = sub_id if payload["plan"] == "pro" else payload.get("stripe_subscription_id")
    payload["email"] = user.get("email")

    await supabase_patch("user_profiles", {"id": f"eq.{user_id}"}, payload)
    return {"success": True, **payload}


@app.post("/create-billing-portal")
async def create_billing_portal(request: Request):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe config missing.")

    user = await get_authenticated_user(request)
    profile = await get_or_create_user_profile(user["id"], user.get("email"))

    stripe_customer_id = profile.get("stripe_customer_id")
    if not stripe_customer_id:
        raise HTTPException(status_code=400, detail="No Stripe customer found")

    session = stripe.billing_portal.Session.create(
        customer=stripe_customer_id,
        return_url=f"{APP_BASE_URL}/dashboard.html",
    )
    return {"url": session.url}


@app.post("/refresh-subscription")
async def refresh_subscription(request: Request):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe config missing.")

    user = await get_authenticated_user(request)
    profile = await get_or_create_user_profile(user["id"], user.get("email"))

    sub_id = profile.get("stripe_subscription_id")
    customer_id = profile.get("stripe_customer_id")

    if not sub_id:
        payload = {
            "plan": "free",
            "max_file_size_mb": 5,
            "daily_conversion_limit": 2,
            "subscription_status": profile.get("subscription_status") or "inactive",
            "subscription_cancel_at_period_end": False,
            "current_period_end": None,
        }
        await supabase_patch("user_profiles", {"id": f"eq.{user['id']}"}, payload)
        return {"ok": True, **payload}

    sub = stripe.Subscription.retrieve(sub_id)
    payload = plan_payload_from_subscription(sub)
    payload["stripe_customer_id"] = customer_id or payload.get("stripe_customer_id")
    payload["email"] = user.get("email")
    await supabase_patch("user_profiles", {"id": f"eq.{user['id']}"}, payload)

    return {"ok": True, **payload}


@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Webhook secret missing.")

    payload = await request.body()
    sig = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    obj = event["data"]["object"]

    if event["type"] == "checkout.session.completed":
        user_id = getattr(obj, "client_reference_id", None)
        customer_id = getattr(obj, "customer", None)
        subscription_id = getattr(obj, "subscription", None)

        if user_id:
            patch_payload = {
                "plan": "pro",
                "max_file_size_mb": 50,
                "daily_conversion_limit": 50,
                "subscription_status": "active",
                "subscription_cancel_at_period_end": False,
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": subscription_id,
            }
            if subscription_id:
                try:
                    sub = stripe.Subscription.retrieve(subscription_id)
                    patch_payload.update(plan_payload_from_subscription(sub))
                    patch_payload["stripe_customer_id"] = customer_id or patch_payload.get("stripe_customer_id")
                except Exception:
                    pass

            await supabase_patch("user_profiles", {"id": f"eq.{user_id}"}, patch_payload)

    elif event["type"] in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        sub_id = getattr(obj, "id", None)
        customer_id = getattr(obj, "customer", None)

        rows = await supabase_get("/rest/v1/user_profiles", {"stripe_customer_id": f"eq.{customer_id}", "select": "id"})
        if not rows and sub_id:
            rows = await supabase_get("/rest/v1/user_profiles", {"stripe_subscription_id": f"eq.{sub_id}", "select": "id"})

        if rows:
            user_id = rows[0]["id"]
            payload2 = plan_payload_from_subscription(obj)
            payload2["stripe_customer_id"] = customer_id or payload2.get("stripe_customer_id")
            await supabase_patch("user_profiles", {"id": f"eq.{user_id}"}, payload2)

    elif event["type"] == "invoice.payment_failed":
        customer_id = getattr(obj, "customer", None)
        rows = await supabase_get("/rest/v1/user_profiles", {"stripe_customer_id": f"eq.{customer_id}", "select": "id"})
        if rows:
            user_id = rows[0]["id"]
            await supabase_patch(
                "user_profiles",
                {"id": f"eq.{user_id}"},
                {"plan": "free", "max_file_size_mb": 5, "daily_conversion_limit": 2, "subscription_status": "past_due"},
            )

    return {"ok": True}


@app.get("/usage-summary")
async def usage_summary(request: Request):
    user = await get_authenticated_user(request)
    profile = await get_or_create_user_profile(user["id"], user.get("email"))
    used_today = await get_usage_count_last_24h(user["id"])
    daily_limit = int(profile.get("daily_conversion_limit") or 2)
    remaining = max(daily_limit - used_today, 0)

    return {
        "plan": profile.get("plan", "free"),
        "max_file_size_mb": int(profile.get("max_file_size_mb") or 5),
        "daily_conversion_limit": daily_limit,
        "used_last_24h": used_today,
        "remaining_conversions": remaining,
        "subscription_status": profile.get("subscription_status") or "inactive",
        "subscription_cancel_at_period_end": bool(profile.get("subscription_cancel_at_period_end", False)),
        "current_period_end": profile.get("current_period_end"),
        "stripe_customer_id": profile.get("stripe_customer_id"),
        "stripe_subscription_id": profile.get("stripe_subscription_id"),
    }


@app.post("/convert-txt")
async def convert_txt(request: Request, file: UploadFile = File(...)):
    try:
        if not SUPABASE_URL or not SUPABASE_ANON_KEY or not SUPABASE_SERVICE_ROLE_KEY:
            raise HTTPException(status_code=500, detail="Backend pricing config is missing.")

        filename = file.filename or ""
        content = await file.read()

        if len(content) > ABSOLUTE_MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="Current system max is 50 MB.")

        user = await get_authenticated_user(request)
        profile = await get_or_create_user_profile(user["id"], user.get("email"))
        used_today = await get_usage_count_last_24h(user["id"])

        max_bytes = int(profile.get("max_file_size_mb") or 5) * 1024 * 1024
        daily_limit = int(profile.get("daily_conversion_limit") or 2)
        plan_name = profile.get("plan", "free")

        if len(content) > max_bytes:
            raise HTTPException(status_code=400, detail=f"{plan_name.capitalize()} plan limit is {profile.get('max_file_size_mb', 5)} MB.")

        if used_today >= daily_limit:
            raise HTTPException(status_code=403, detail=f"{plan_name.capitalize()} plan limit reached: {daily_limit} conversions per 24 hours.")

        source_name, text = extract_chat_text(filename, content)
        items = parse_whatsapp_text(text)
        if not items:
            raise HTTPException(status_code=400, detail="Could not parse any messages from this file.")

        chunks = chunk_items(items, chunk_size=80)
        writer = PdfWriter()

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                total_chunks = len(chunks)
                for idx, chunk in enumerate(chunks, start=1):
                    pdf_bytes = await render_chunk_pdf(browser, source_name, chunk, idx, total_chunks)
                    reader = PdfReader(BytesIO(pdf_bytes))
                    for page in reader.pages:
                        writer.add_page(page)
            finally:
                await browser.close()

        output_buffer = BytesIO()
        writer.write(output_buffer)
        output_buffer.seek(0)

        await record_usage_event(user["id"])

        output_name = f"{source_name}.pdf"
        content_disposition = build_download_header(output_name)

        return StreamingResponse(
            output_buffer,
            media_type="application/pdf",
            headers={"Content-Disposition": content_disposition},
        )

    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
