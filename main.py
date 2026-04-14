from io import BytesIO
import asyncio
import base64
import html
import os
import re
import zipfile
from typing import List, Dict
from urllib.parse import quote
from datetime import datetime, timedelta, timezone

import httpx
import stripe
from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Query
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
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ABSOLUTE_MAX_FILE_SIZE = 50 * 1024 * 1024  # hard system cap 50 MB

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID_PRO_MONTHLY = os.getenv("STRIPE_PRICE_ID_PRO_MONTHLY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
APP_BASE_URL = os.getenv("APP_BASE_URL", "https://www.wachatprint.com")

stripe.api_key = STRIPE_SECRET_KEY

FREE_PLAN_LIMIT_MB = 5
FREE_PLAN_DAILY_LIMIT = 2
PRO_PLAN_LIMIT_MB = 50
PRO_PLAN_DAILY_LIMIT = 50

PATTERN_BRACKET = re.compile(
    r"^\[(?P<date>[^,\]]+),\s*(?P<time>[^\]]+)\]\s*(?P<rest>.+)$"
)

PATTERN_DASH = re.compile(
    r"^(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{2,4}),\s*(?P<time>[^-]+?)\s*-\s*(?P<rest>.+)$"
)

ATTACHED_TAG_RE = re.compile(
    r"^<attached:\s*(?P<name>[^>]+)>\s*(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)

FILE_ATTACHED_RE = re.compile(
    r"^(?P<name>.+?)\s*\((?:file|image|video|audio|document)\s+attached\)\s*(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)

IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

MAX_INLINE_IMAGE_BYTES = 2 * 1024 * 1024
MAX_INLINE_IMAGES = 40


@app.get("/")
def root():
    return {"message": "WAChatPrint API is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/stripe-webhook")
def stripe_webhook_status():
    return {"status": "ready"}


def stripe_object_to_dict(obj):
    if hasattr(obj, "to_dict_recursive"):
        return obj.to_dict_recursive()
    return obj


def log_stripe(message: str, **kwargs):
    try:
        details = " ".join(f"{key}={value}" for key, value in kwargs.items())
        print(f"[WAChatPrint Stripe] {message} {details}".strip())
    except Exception:
        print(f"[WAChatPrint Stripe] {message}")


def split_sender_and_message(rest: str):
    if ": " in rest:
        sender, message = rest.split(": ", 1)
        return sender.strip(), message.strip(), False
    return None, rest.strip(), True


def format_message_html(text: str) -> str:
    safe = html.escape(text)
    return safe.replace("\n", "<br>")


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


def format_attachment_size(size_bytes):
    if size_bytes is None:
        return ""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.2f} MB"


def build_media_index_from_zip(zip_file: zipfile.ZipFile) -> Dict[str, Dict]:
    media_index: Dict[str, Dict] = {}
    inline_images_used = 0

    for name in zip_file.namelist():
        if name.endswith("/"):
            continue

        lower_name = name.lower()
        if "__macosx" in lower_name:
            continue

        base_name = os.path.basename(name)
        if not base_name:
            continue

        ext = os.path.splitext(base_name)[1].lower()
        info = zip_file.getinfo(name)

        entry = {
            "filename": base_name,
            "size_bytes": info.file_size,
            "kind": "file",
            "data_url": None,
        }

        if ext in IMAGE_MIME_TYPES:
            entry["kind"] = "image"
            if info.file_size <= MAX_INLINE_IMAGE_BYTES and inline_images_used < MAX_INLINE_IMAGES:
                raw = zip_file.read(name)
                encoded = base64.b64encode(raw).decode("ascii")
                entry["data_url"] = f"data:{IMAGE_MIME_TYPES[ext]};base64,{encoded}"
                inline_images_used += 1

        media_index[base_name.lower()] = entry

    return media_index


def extract_attachment_reference(message: str):
    text = (message or "").strip()

    match = ATTACHED_TAG_RE.match(text)
    if match:
        return match.group("name").strip(), match.group("rest").strip()

    match = FILE_ATTACHED_RE.match(text)
    if match:
        return match.group("name").strip(), match.group("rest").strip()

    return None, message


def annotate_message_attachment(message: str, media_index: Dict[str, Dict]):
    filename, cleaned_message = extract_attachment_reference(message)

    if not filename:
        return message, None

    key = filename.lower()
    media = media_index.get(key)

    ext = os.path.splitext(filename)[1].lower()
    fallback_kind = "image" if ext in IMAGE_MIME_TYPES else "file"

    if media:
        return cleaned_message, {
            "kind": media["kind"],
            "filename": media["filename"],
            "size_bytes": media["size_bytes"],
            "data_url": media.get("data_url"),
        }

    return cleaned_message, {
        "kind": fallback_kind,
        "filename": filename,
        "size_bytes": None,
        "data_url": None,
    }


def parse_whatsapp_text(text: str, media_index: Dict[str, Dict] | None = None) -> List[Dict]:
    messages: List[Dict] = []
    sender_side_map: Dict[str, str] = {}
    last_date = None
    media_index = media_index or {}

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
            attachment = None

            if not is_system:
                message, attachment = annotate_message_attachment(message, media_index)

            if sender:
                if sender not in sender_side_map:
                    sender_side_map[sender] = "left" if len(sender_side_map) % 2 == 0 else "right"
                side = sender_side_map[sender]
            else:
                side = "center"

            if date != last_date:
                messages.append(
                    {
                        "type": "date_separator",
                        "date": date,
                    }
                )
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
                    "attachment": attachment,
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
                        "attachment": None,
                    }
                )

    return messages


def build_attachment_html(attachment: Dict | None) -> str:
    if not attachment:
        return ""

    filename = html.escape(attachment.get("filename", "attachment"))
    size_label = format_attachment_size(attachment.get("size_bytes"))
    size_html = f" · {html.escape(size_label)}" if size_label else ""

    if attachment.get("kind") == "image" and attachment.get("data_url"):
        data_url = html.escape(attachment["data_url"], quote=True)
        return f"""
        <div class="media-block">
          <img class="chat-image" src="{data_url}" alt="{filename}" />
          <div class="attachment-label">Image attached: {filename}{size_html}</div>
        </div>
        """

    if attachment.get("kind") == "image":
        return f"""
        <div class="attachment-box">
          <div class="attachment-title">Image attached</div>
          <div class="attachment-sub">{filename}{size_html}</div>
        </div>
        """

    return f"""
    <div class="attachment-box">
      <div class="attachment-title">Attachment</div>
      <div class="attachment-sub">{filename}{size_html}</div>
    </div>
    """


def extract_chat_payload(filename: str, content: bytes):
    lower_name = (filename or "").lower()

    if lower_name.endswith(".txt"):
        source_name = os.path.splitext(os.path.basename(filename))[0]
        return source_name, decode_text_bytes(content), {}

    if lower_name.endswith(".zip"):
        try:
            with zipfile.ZipFile(BytesIO(content)) as zf:
                chosen_txt = choose_txt_from_zip(zf)
                txt_bytes = zf.read(chosen_txt)
                source_name = os.path.splitext(os.path.basename(chosen_txt))[0]
                media_index = build_media_index_from_zip(zf)
                return source_name, decode_text_bytes(txt_bytes), media_index
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

        attachment_html = build_attachment_html(msg.get("attachment"))
        message_text_html = ""
        if (msg.get("message") or "").strip():
            message_text_html = f'<div class="message-text">{format_message_html(msg["message"])}</div>'

        chunks.append(
            f"""
            <div class="msg-row {side_class}">
              <div class="bubble {side_class}">
                {sender_html}
                {attachment_html}
                {message_text_html}
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
    @page {{
      size: A4;
      margin: 10mm 8mm 12mm 8mm;
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      font-family: "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji",
                   "Segoe UI", Arial, sans-serif;
      background: #efeae2;
      color: #111827;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }}

    .topbar {{
      background: #0f8f7d;
      color: white;
      padding: 14px 16px;
      font-weight: 700;
      font-size: 18px;
      border-radius: 10px;
      margin-bottom: 10px;
    }}

    .chat-card {{
      background: #f6f1ea;
      border-radius: 14px;
      padding: 10px;
    }}

    .chat-title {{
      background: white;
      border-radius: 12px;
      padding: 12px 14px;
      margin-bottom: 12px;
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      font-size: 13px;
      color: #374151;
    }}

    .chat-title strong {{
      color: #111827;
      font-size: 14px;
    }}

    .date-separator-wrap,
    .system-wrap {{
      text-align: center;
      margin: 10px 0;
    }}

    .date-separator {{
      display: inline-block;
      background: #dbeafe;
      color: #1d4ed8;
      padding: 6px 12px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
    }}

    .system-message {{
      display: inline-block;
      max-width: 70%;
      background: #d1d5db;
      color: #374151;
      padding: 8px 12px;
      border-radius: 10px;
      font-size: 12px;
      line-height: 1.5;
    }}

    .system-time {{
      margin-top: 4px;
      font-size: 10px;
      color: #6b7280;
    }}

    .msg-row {{
      display: flex;
      margin: 7px 0;
      width: 100%;
      break-inside: avoid;
      page-break-inside: avoid;
    }}

    .msg-row.left {{
      justify-content: flex-start;
    }}

    .msg-row.right {{
      justify-content: flex-end;
    }}

    .bubble {{
      max-width: 72%;
      padding: 8px 10px 6px;
      border-radius: 12px;
      line-height: 1.45;
      word-break: break-word;
      white-space: normal;
      break-inside: avoid;
      page-break-inside: avoid;
    }}

    .bubble.left {{
      background: white;
      border-top-left-radius: 4px;
    }}

    .bubble.right {{
      background: #dcf8c6;
      border-top-right-radius: 4px;
    }}

    .sender {{
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 4px;
      color: #2563eb;
    }}

    .bubble.right .sender {{
      color: #0f8f7d;
    }}

    .media-block {{
      margin-bottom: 6px;
    }}

    .chat-image {{
      display: block;
      max-width: 220px;
      max-height: 220px;
      width: auto;
      height: auto;
      border-radius: 10px;
      border: 1px solid rgba(15, 23, 42, 0.08);
      background: #f8fafc;
    }}

    .attachment-label {{
      margin-top: 6px;
      font-size: 11px;
      color: #475569;
    }}

    .attachment-box {{
      margin-bottom: 6px;
      padding: 10px;
      border-radius: 10px;
      background: rgba(15, 23, 42, 0.05);
    }}

    .attachment-title {{
      font-size: 12px;
      font-weight: 700;
      color: #0f172a;
    }}

    .attachment-sub {{
      font-size: 11px;
      color: #475569;
      margin-top: 2px;
    }}

    .message-text {{
      font-size: 13px;
      color: #111827;
    }}

    .meta {{
      margin-top: 4px;
      text-align: right;
      font-size: 10px;
      color: #6b7280;
    }}
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
        pdf_bytes = await page.pdf(
            format="A4",
            print_background=True,
            margin={
                "top": "10mm",
                "right": "8mm",
                "bottom": "12mm",
                "left": "8mm",
            },
            prefer_css_page_size=True,
        )
        return pdf_bytes
    finally:
        await page.close()


async def get_authenticated_user(request: Request):
    auth_header = request.headers.get("authorization", "")

    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Login required.")

    access_token = auth_header.split(" ", 1)[1].strip()

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {access_token}",
            },
        )

    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid session. Please log in again.")

    return response.json()


async def supabase_get(table: str, params: dict):
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            params=params,
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
        )
    if response.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Could not read {table}.")
    return response.json()


async def supabase_patch(table: str, filters: dict, payload: dict):
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.patch(
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
    if response.status_code not in (200, 204):
        raise HTTPException(status_code=500, detail=f"Could not update {table}.")


async def supabase_insert(table: str, payload: dict):
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            json=payload,
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )
    if response.status_code not in (200, 201, 204):
        raise HTTPException(status_code=500, detail=f"Could not insert into {table}.")


async def supabase_upsert(table: str, payload: dict, on_conflict: str):
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            params={"on_conflict": on_conflict},
            json=payload,
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
        )
    if response.status_code not in (200, 201, 204):
        raise HTTPException(
            status_code=500,
            detail=f"Could not upsert {table}: {response.text}"
        )


async def upsert_user_profile(user_id: str, payload: dict):
    row = {"id": user_id, **payload}
    await supabase_upsert("user_profiles", row, on_conflict="id")


async def get_user_profile(user_id: str):
    rows = await supabase_get(
        "user_profiles",
        {
            "id": f"eq.{user_id}",
            "select": "plan,max_file_size_mb,daily_conversion_limit,stripe_customer_id,stripe_subscription_id,subscription_status,current_period_end",
        },
    )

    if not rows:
        default_profile = {
            "plan": "free",
            "max_file_size_mb": FREE_PLAN_LIMIT_MB,
            "daily_conversion_limit": FREE_PLAN_DAILY_LIMIT,
            "stripe_customer_id": None,
            "stripe_subscription_id": None,
            "subscription_status": "inactive",
            "current_period_end": None,
        }
        await upsert_user_profile(user_id, default_profile)
        return default_profile

    return rows[0]


async def get_profile_by_customer_id(customer_id: str):
    rows = await supabase_get(
        "user_profiles",
        {
            "stripe_customer_id": f"eq.{customer_id}",
            "select": "id",
        },
    )
    return rows[0] if rows else None


async def get_profile_by_subscription_id(subscription_id: str):
    rows = await supabase_get(
        "user_profiles",
        {
            "stripe_subscription_id": f"eq.{subscription_id}",
            "select": "id",
        },
    )
    return rows[0] if rows else None


async def get_usage_count_last_24h(user_id: str) -> int:
    since = datetime.now(timezone.utc) - timedelta(days=1)

    rows = await supabase_get(
        "conversion_usage",
        {
            "user_id": f"eq.{user_id}",
            "created_at": f"gte.{since.isoformat()}",
            "select": "id",
        },
    )
    return len(rows)


async def build_usage_summary(user_id: str):
    profile = await get_user_profile(user_id)
    used_last_24h = await get_usage_count_last_24h(user_id)

    daily_limit = int(profile["daily_conversion_limit"])
    used_last_24h = int(used_last_24h)

    return {
        "plan": profile["plan"],
        "max_file_size_mb": int(profile["max_file_size_mb"]),
        "daily_conversion_limit": daily_limit,
        "used_last_24h": used_last_24h,
        "remaining_today": max(daily_limit - used_last_24h, 0),
        "subscription_status": profile.get("subscription_status") or "inactive",
        "current_period_end": profile.get("current_period_end"),
    }


async def record_usage_event(user_id: str):
    await supabase_insert("conversion_usage", {"user_id": user_id})


def pro_profile_payload(subscription_status: str, stripe_customer_id: str | None, stripe_subscription_id: str | None, current_period_end: str | None):
    return {
        "plan": "pro",
        "max_file_size_mb": PRO_PLAN_LIMIT_MB,
        "daily_conversion_limit": PRO_PLAN_DAILY_LIMIT,
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": stripe_subscription_id,
        "subscription_status": subscription_status,
        "current_period_end": current_period_end,
    }


def free_profile_payload(subscription_status: str, stripe_customer_id: str | None, current_period_end: str | None):
    return {
        "plan": "free",
        "max_file_size_mb": FREE_PLAN_LIMIT_MB,
        "daily_conversion_limit": FREE_PLAN_DAILY_LIMIT,
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": None,
        "subscription_status": subscription_status,
        "current_period_end": current_period_end,
    }


def to_iso_from_unix(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


async def sync_user_profile_from_checkout_session(user_id: str, session_id: str):
    session = await asyncio.to_thread(
        stripe.checkout.Session.retrieve,
        session_id,
        expand=["subscription"],
    )
    session_data = stripe_object_to_dict(session)

    session_user_id = session_data.get("client_reference_id") or session_data.get("metadata", {}).get("user_id")
    if session_user_id != user_id:
        raise HTTPException(status_code=403, detail="This checkout session does not belong to the logged-in user.")

    if session_data.get("mode") != "subscription":
        raise HTTPException(status_code=400, detail="This checkout session is not a subscription checkout.")

    customer_id = session_data.get("customer")
    subscription = session_data.get("subscription")
    subscription_data = stripe_object_to_dict(subscription) if isinstance(subscription, dict) or hasattr(subscription, "to_dict_recursive") else None
    subscription_id = None
    subscription_status = "inactive"
    current_period_end = None

    if isinstance(subscription, str):
        subscription_id = subscription
        stripe_sub = await asyncio.to_thread(stripe.Subscription.retrieve, subscription_id)
        subscription_data = stripe_object_to_dict(stripe_sub)

    if subscription_data:
        subscription_id = subscription_data.get("id")
        subscription_status = subscription_data.get("status") or subscription_status
        current_period_end = to_iso_from_unix(subscription_data.get("current_period_end"))

    if session_data.get("payment_status") == "paid" and subscription_status in ("active", "trialing", "past_due", "incomplete"):
        await upsert_user_profile(
            user_id,
            pro_profile_payload(
                subscription_status="active" if subscription_status == "incomplete" else subscription_status,
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
                current_period_end=current_period_end,
            ),
        )
    else:
        await upsert_user_profile(
            user_id,
            free_profile_payload(
                subscription_status=subscription_status,
                stripe_customer_id=customer_id,
                current_period_end=current_period_end,
            ),
        )

    return await build_usage_summary(user_id)


@app.get("/verify-checkout-session")
async def verify_checkout_session(request: Request, session_id: str = Query(...)):
    try:
        if not STRIPE_SECRET_KEY:
            raise HTTPException(status_code=500, detail="Stripe config is missing.")

        user = await get_authenticated_user(request)
        summary = await sync_user_profile_from_checkout_session(user["id"], session_id)
        return {
            "ok": True,
            "message": "Plan synced successfully.",
            "summary": summary,
        }
    except HTTPException:
        raise
    except Exception as e:
        log_stripe("Checkout verification failed", session_id=session_id, error=repr(e))
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/usage-summary")
async def usage_summary(request: Request):
    try:
        if not SUPABASE_URL or not SUPABASE_ANON_KEY or not SUPABASE_SERVICE_ROLE_KEY:
            raise HTTPException(status_code=500, detail="Backend pricing config is missing.")

        user = await get_authenticated_user(request)
        return await build_usage_summary(user["id"])
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/create-checkout-session")
async def create_checkout_session(request: Request):
    try:
        if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID_PRO_MONTHLY:
            raise HTTPException(status_code=500, detail="Stripe config is missing.")

        user = await get_authenticated_user(request)
        profile = await get_user_profile(user["id"])

        if profile.get("subscription_status") in ("active", "trialing", "past_due"):
            raise HTTPException(status_code=400, detail="You already have an active paid plan. Use Manage Billing.")

        params = {
            "mode": "subscription",
            "success_url": f"{APP_BASE_URL}/billing-success.html?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{APP_BASE_URL}/billing-cancel.html",
            "line_items": [
                {
                    "price": STRIPE_PRICE_ID_PRO_MONTHLY,
                    "quantity": 1,
                }
            ],
            "client_reference_id": user["id"],
            "metadata": {
                "user_id": user["id"],
                "email": user.get("email", ""),
                "product": "WAChatPrint Pro",
            },
            "subscription_data": {
                "metadata": {
                    "user_id": user["id"],
                }
            },
            "allow_promotion_codes": True,
        }

        if profile.get("stripe_customer_id"):
            params["customer"] = profile["stripe_customer_id"]
        else:
            params["customer_email"] = user.get("email", "")

        session = await asyncio.to_thread(stripe.checkout.Session.create, **params)
        return {"url": session.url}
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/create-portal-session")
async def create_portal_session(request: Request):
    try:
        if not STRIPE_SECRET_KEY:
            raise HTTPException(status_code=500, detail="Stripe config is missing.")

        user = await get_authenticated_user(request)
        profile = await get_user_profile(user["id"])

        if not profile.get("stripe_customer_id"):
            raise HTTPException(status_code=400, detail="No billing profile found yet. Upgrade first.")

        session = await asyncio.to_thread(
            stripe.billing_portal.Session.create,
            customer=profile["stripe_customer_id"],
            return_url=f"{APP_BASE_URL}/dashboard.html",
        )
        return {"url": session.url}
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    try:
        if not STRIPE_WEBHOOK_SECRET:
            raise HTTPException(status_code=500, detail="Stripe webhook secret is missing.")

        payload = await request.body()
        sig_header = request.headers.get("stripe-signature")

        event = await asyncio.to_thread(
            stripe.Webhook.construct_event,
            payload,
            sig_header,
            STRIPE_WEBHOOK_SECRET,
        )

        event_data = stripe_object_to_dict(event)
        event_type = event_data.get("type")
        obj = stripe_object_to_dict(event_data.get("data", {}).get("object", {}))

        log_stripe(
            "Webhook received",
            event_type=event_type,
            event_id=event_data.get("id"),
        )

        if event_type == "checkout.session.completed":
            if obj.get("mode") == "subscription":
                user_id = obj.get("client_reference_id") or obj.get("metadata", {}).get("user_id")
                if user_id:
                    await upsert_user_profile(
                        user_id,
                        pro_profile_payload(
                            subscription_status="active",
                            stripe_customer_id=obj.get("customer"),
                            stripe_subscription_id=obj.get("subscription"),
                            current_period_end=None,
                        ),
                    )

        elif event_type == "customer.subscription.updated":
            sub_id = obj.get("id")
            customer_id = obj.get("customer")
            status = obj.get("status")
            period_end = to_iso_from_unix(obj.get("current_period_end"))

            profile = await get_profile_by_subscription_id(sub_id)
            if not profile and customer_id:
                profile = await get_profile_by_customer_id(customer_id)

            if profile:
                if status in ("active", "trialing", "past_due", "incomplete"):
                    await upsert_user_profile(
                        profile["id"],
                        pro_profile_payload(
                            subscription_status="active" if status == "incomplete" else status,
                            stripe_customer_id=customer_id,
                            stripe_subscription_id=sub_id,
                            current_period_end=period_end,
                        ),
                    )
                else:
                    await upsert_user_profile(
                        profile["id"],
                        free_profile_payload(
                            subscription_status=status or "inactive",
                            stripe_customer_id=customer_id,
                            current_period_end=period_end,
                        ),
                    )

        elif event_type == "customer.subscription.deleted":
            sub_id = obj.get("id")
            customer_id = obj.get("customer")
            period_end = to_iso_from_unix(obj.get("current_period_end"))

            profile = await get_profile_by_subscription_id(sub_id)
            if not profile and customer_id:
                profile = await get_profile_by_customer_id(customer_id)

            if profile:
                await upsert_user_profile(
                    profile["id"],
                    free_profile_payload(
                        subscription_status="canceled",
                        stripe_customer_id=customer_id,
                        current_period_end=period_end,
                    ),
                )

        return {"received": True}
    except HTTPException:
        raise
    except Exception as e:
        log_stripe("Webhook failed", error=repr(e))
        return JSONResponse(status_code=400, content={"error": str(e)})


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
        usage = await build_usage_summary(user["id"])

        max_bytes = int(usage["max_file_size_mb"]) * 1024 * 1024
        daily_limit = int(usage["daily_conversion_limit"])
        plan_name = usage["plan"]
        used_today = int(usage["used_last_24h"])

        if len(content) > max_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"{plan_name.capitalize()} plan limit is {usage['max_file_size_mb']} MB."
            )

        if used_today >= daily_limit:
            raise HTTPException(
                status_code=403,
                detail=f"{plan_name.capitalize()} plan limit reached: {daily_limit} conversions per 24 hours."
            )

        source_name, text, media_index = extract_chat_payload(filename, content)
        items = parse_whatsapp_text(text, media_index)

        if not items:
            raise HTTPException(status_code=400, detail="Could not parse any messages from this file.")

        chunks = chunk_items(items, chunk_size=80)
        writer = PdfWriter()

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )

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
            headers={"Content-Disposition": content_disposition}
        )

    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
