from io import BytesIO
import html
import os
import re
import zipfile
from typing import List, Dict
from urllib.parse import quote
from datetime import datetime, timedelta, timezone

import httpx
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
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ABSOLUTE_MAX_FILE_SIZE = 50 * 1024 * 1024  # hard system cap 50 MB

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

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
                <div class=\"date-separator-wrap\">
                  <div class=\"date-separator\">{html.escape(item["date"])}</div>
                </div>
                """
            )
            continue

        msg = item

        if msg["is_system"]:
            chunks.append(
                f"""
                <div class=\"system-wrap\">
                  <div class=\"system-message\">
                    <div class=\"system-text\">{format_message_html(msg["message"])}</div>
                    <div class=\"system-time\">{html.escape(msg["time"] or "")}</div>
                  </div>
                </div>
                """
            )
            continue

        side_class = "left" if msg["side"] == "left" else "right"
        sender_html = (
            f'<div class=\"sender\">{html.escape(msg["sender"])}</div>'
            if msg["sender"]
            else ""
        )

        chunks.append(
            f"""
            <div class=\"msg-row {side_class}\">
              <div class=\"bubble {side_class}\">
                {sender_html}
                <div class=\"message-text\">{format_message_html(msg["message"])}</div>
                <div class=\"meta\">{html.escape(msg["time"] or "")}</div>
              </div>
            </div>
            """
        )

    chat_body = "\n".join(chunks)

    return f"""
<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"UTF-8\" />
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
  <div class=\"topbar\">WAChatPrint</div>

  <div class=\"chat-card\">
    <div class=\"chat-title\">
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


async def get_user_profile(user_id: str):
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{SUPABASE_URL}/rest/v1/user_profiles",
            params={
                "id": f"eq.{user_id}",
                "select": "plan,max_file_size_mb,daily_conversion_limit",
            },
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
        )

    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="Could not read plan profile.")

    rows = response.json()
    if not rows:
        return {
            "plan": "free",
            "max_file_size_mb": 5,
            "daily_conversion_limit": 2,
        }

    return rows[0]


async def get_usage_count_last_24h(user_id: str) -> int:
    since = datetime.now(timezone.utc) - timedelta(days=1)

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{SUPABASE_URL}/rest/v1/conversion_usage",
            params={
                "user_id": f"eq.{user_id}",
                "created_at": f"gte.{since.isoformat()}",
                "select": "id",
            },
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
        )

    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="Could not read usage history.")

    return len(response.json())


async def record_usage_event(user_id: str):
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{SUPABASE_URL}/rest/v1/conversion_usage",
            json={"user_id": user_id},
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )

    if response.status_code not in (200, 201, 204):
        raise HTTPException(status_code=500, detail="Could not record usage.")


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
        profile = await get_user_profile(user["id"])
        used_today = await get_usage_count_last_24h(user["id"])

        max_bytes = int(profile["max_file_size_mb"]) * 1024 * 1024
        daily_limit = int(profile["daily_conversion_limit"])
        plan_name = profile["plan"]

        if len(content) > max_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"{plan_name.capitalize()} plan limit is {profile['max_file_size_mb']} MB."
            )

        if used_today >= daily_limit:
            raise HTTPException(
                status_code=403,
                detail=f"{plan_name.capitalize()} plan limit reached: {daily_limit} conversions per 24 hours."
            )

        source_name, text = extract_chat_text(filename, content)
        items = parse_whatsapp_text(text)

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
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )
