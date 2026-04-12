from io import BytesIO
import html
import re
from typing import List, Dict

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from playwright.async_api import async_playwright


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

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB

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


def chunk_items(items: List[Dict], chunk_size: int = 180) -> List[List[Dict]]:
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


def build_chat_html(source_name: str, items: List[Dict]) -> str:
    pages = []
    item_chunks = chunk_items(items, chunk_size=180)

    for chunk in item_chunks:
        chunks: List[str] = []

        for item in chunk:
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

        page_html = "\n".join(chunks)
        pages.append(
            f"""
            <section class="print-page">
              <div class="chat-card">
                <div class="chat-title">
                  <div><strong>{html.escape(source_name)}</strong></div>
                  <div>Exported WhatsApp chat → PDF</div>
                </div>
                {page_html}
              </div>
            </section>
            """
        )

    pages_html = "\n".join(pages)

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

    .print-page {{
      page-break-after: always;
      break-after: page;
      min-height: 260mm;
    }}

    .print-page:last-child {{
      page-break-after: auto;
      break-after: auto;
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
  <div class="topbar">WAChatPrint</div>
  {pages_html}
</body>
</html>
    """


@app.post("/convert-txt")
async def convert_txt(file: UploadFile = File(...)):
    try:
        filename = file.filename or ""

        if not filename.lower().endswith(".txt"):
            raise HTTPException(status_code=400, detail="Only .txt files are allowed for now.")

        content = await file.read()

        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="File is too large. Max 5 MB allowed.")

        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = content.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = content.decode("latin-1", errors="ignore")

        items = parse_whatsapp_text(text)

        if not items:
            raise HTTPException(status_code=400, detail="Could not parse any messages from this file.")

        source_name = filename.rsplit(".", 1)[0]
        html_content = build_chat_html(source_name, items)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            page = await browser.new_page()
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
            await browser.close()

        output_name = filename.rsplit(".", 1)[0] + ".pdf"

        return StreamingResponse(
            BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{output_name}"'}
        )

    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )
