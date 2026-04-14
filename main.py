from io import BytesIO
import base64
import html
import mimetypes
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
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

ATTACHED_NAME_PATTERN = re.compile(
    r"^(?:<attached:\s*)?(?P<name>[^>\n]+?\.[A-Za-z0-9]{2,6})(?:>)?"
    r"(?:\s*\((?:file|image|video|audio|document|media|sticker)\s+attached\))?"
    r"(?:\s*[·•]\s*(?P<size>[\d.]+\s*(?:KB|MB|GB)))?$",
    re.IGNORECASE,
)

LABELLED_ATTACHMENT_PATTERN = re.compile(
    r"^(?P<label>image|video|audio|voice note|voice|document|attachment|sticker|gif)"
    r"\s+attached:?\s*(?P<name>.+?)(?:\s*[·•]\s*(?P<size>[\d.]+\s*(?:KB|MB|GB)))?$",
    re.IGNORECASE,
)

LABEL_ONLY_PATTERN = re.compile(
    r"^(?P<label>image|video|audio|voice note|voice|document|attachment|sticker|gif)(?:\s+attached)?$",
    re.IGNORECASE,
)

OMITTED_PATTERN = re.compile(
    r"^(?P<label>image|video|audio|voice note|voice|document|sticker|gif|media)\s+omitted$",
    re.IGNORECASE,
)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
AUDIO_EXTENSIONS = {".opus", ".ogg", ".mp3", ".m4a", ".aac", ".wav", ".amr"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".3gp", ".mkv", ".webm"}
DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".csv", ".zip", ".rar"
}

EXPORT_FORMATS = {"pdf", "pdf_media_zip", "html_zip"}


@app.get("/")
def root():
    return {"message": "WAChatPrint API is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


def split_sender_and_message(rest: str) -> Tuple[Optional[str], str, bool]:
    if ": " in rest:
        sender, message = rest.split(": ", 1)
        return sender.strip(), message.strip(), False
    return None, rest.strip(), True


def clean_line(value: str) -> str:
    return value.replace("\u200e", "").replace("\u200f", "").strip()


def format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} bytes"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    if value < 1024 * 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB"
    return f"{value / (1024 * 1024 * 1024):.1f} GB"


def guess_media_kind(filename: str, label: Optional[str] = None) -> str:
    lower_name = (filename or "").lower()
    ext = os.path.splitext(lower_name)[1]

    if label:
        label = label.lower().strip()
        if label in {"voice note", "voice"}:
            return "voice"
        if label in {"image", "video", "audio", "document", "attachment", "sticker", "gif"}:
            return "attachment" if label == "attachment" else label

    if lower_name.startswith("ptt-") or ext == ".opus":
        return "voice"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in DOCUMENT_EXTENSIONS:
        return "document"
    return "attachment"


def guess_content_type(filename: str) -> str:
    lower_name = (filename or "").lower()
    if lower_name.endswith(".opus"):
        return "audio/ogg"
    content_type, _ = mimetypes.guess_type(filename or "")
    return content_type or "application/octet-stream"


def safe_media_filename(filename: str, used_names: set) -> str:
    basename = os.path.basename(filename or "file")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", basename).strip("._")
    if not safe:
        safe = "file"

    stem, ext = os.path.splitext(safe)
    candidate = safe
    counter = 2
    while candidate.lower() in used_names:
        candidate = f"{stem}_{counter}{ext}"
        counter += 1

    used_names.add(candidate.lower())
    return candidate


def build_media_index(zf: zipfile.ZipFile, chosen_txt: str) -> Dict[str, Dict]:
    media_index: Dict[str, Dict] = {}
    used_output_names: set = set()

    for name in zf.namelist():
        lower_name = name.lower()

        if (
            name.endswith("/")
            or lower_name == chosen_txt.lower()
            or "__macosx" in lower_name
            or os.path.basename(lower_name) in {".ds_store", "thumbs.db"}
        ):
            continue

        basename = os.path.basename(name)
        if not basename:
            continue

        if basename.lower() in media_index:
            continue

        file_bytes = zf.read(name)
        output_name = safe_media_filename(basename, used_output_names)

        media_index[basename.lower()] = {
            "basename": basename,
            "zip_name": name,
            "bytes": file_bytes,
            "size_bytes": len(file_bytes),
            "output_name": output_name,
            "relative_path": f"media/{quote(output_name)}",
            "content_type": guess_content_type(basename),
            "kind": guess_media_kind(basename),
        }

    return media_index


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
                    "media": None,
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
                        "media": None,
                    }
                )

    return messages


def format_message_html(text: str) -> str:
    safe = html.escape(text or "")
    return safe.replace("\n", "<br>")


def build_download_header(filename: str) -> str:
    safe_ascii = "".join(
        ch if (32 <= ord(ch) < 127 and ch not in ['"', "\\"]) else "_"
        for ch in filename
    ).strip()

    if not safe_ascii:
        safe_ascii = "download"

    encoded_utf8 = quote(filename)
    return f'attachment; filename="{safe_ascii}"; filename*=UTF-8\'\'{encoded_utf8}'


def chunk_items(items: List[Dict], chunk_size: int = 60) -> List[List[Dict]]:
    chunks: List[List[Dict]] = []
    current: List[Dict] = []

    for item in items:
        current.append(item)
        if len(current) >= chunk_size:
            if current and current[-1].get("type") == "date_separator":
                current.pop()

            if current:
                chunks.append(current)
                current = []

            if item.get("type") == "date_separator":
                current.append(item)

    if current:
        chunks.append(current)

    return chunks or [[]]


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


def extract_chat_bundle(filename: str, content: bytes) -> Tuple[str, str, Dict[str, Dict]]:
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
                media_index = build_media_index(zf, chosen_txt)
                return source_name, decode_text_bytes(txt_bytes), media_index
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Invalid ZIP file.")

    raise HTTPException(status_code=400, detail="Only .txt and .zip files are supported.")


def parse_filename_line(line: str, forced_label: Optional[str] = None) -> Optional[Dict]:
    cleaned = clean_line(line)
    if not cleaned:
        return None

    labelled_match = LABELLED_ATTACHMENT_PATTERN.match(cleaned)
    if labelled_match:
        raw_name = clean_line(labelled_match.group("name"))
        filename = os.path.basename(raw_name)
        size_text = labelled_match.group("size")
        media_kind = guess_media_kind(filename, labelled_match.group("label"))
        return {
            "filename": filename,
            "display_name": filename,
            "kind": media_kind,
            "size_text": size_text.strip() if size_text else None,
            "source_line_count": 1,
        }

    attached_match = ATTACHED_NAME_PATTERN.match(cleaned)
    if attached_match:
        raw_name = clean_line(attached_match.group("name"))
        filename = os.path.basename(raw_name)
        size_text = attached_match.group("size")
        media_kind = guess_media_kind(filename, forced_label)
        return {
            "filename": filename,
            "display_name": filename,
            "kind": media_kind,
            "size_text": size_text.strip() if size_text else None,
            "source_line_count": 1,
        }

    return None


def extract_media_reference(message_text: str, media_index: Dict[str, Dict]) -> Optional[Dict]:
    raw_lines = [clean_line(line) for line in (message_text or "").splitlines()]
    lines = [line for line in raw_lines if line]

    if not lines:
        return None

    omitted_match = OMITTED_PATTERN.match(lines[0])
    if omitted_match:
        caption = "\n".join(lines[1:]).strip()
        media_kind = guess_media_kind("", omitted_match.group("label"))
        return {
            "kind": media_kind,
            "label": omitted_match.group("label").strip().title(),
            "filename": None,
            "display_name": None,
            "size_text": None,
            "asset": None,
            "missing": True,
            "caption": caption,
        }

    first_line_media = parse_filename_line(lines[0])
    if first_line_media:
        caption = "\n".join(lines[first_line_media["source_line_count"]:]).strip()
        asset = media_index.get((first_line_media["filename"] or "").lower())
        if not first_line_media["size_text"] and asset:
            first_line_media["size_text"] = format_bytes(asset["size_bytes"])

        return {
            "kind": first_line_media["kind"],
            "label": first_line_media["kind"].replace("_", " ").title(),
            "filename": first_line_media["filename"],
            "display_name": first_line_media["display_name"],
            "size_text": first_line_media["size_text"],
            "asset": asset,
            "missing": asset is None,
            "caption": caption,
        }

    label_only_match = LABEL_ONLY_PATTERN.match(lines[0])
    if label_only_match and len(lines) >= 2:
        second_line_media = parse_filename_line(lines[1], forced_label=label_only_match.group("label"))
        if second_line_media:
            caption = "\n".join(lines[2:]).strip()
            asset = media_index.get((second_line_media["filename"] or "").lower())
            if not second_line_media["size_text"] and asset:
                second_line_media["size_text"] = format_bytes(asset["size_bytes"])

            return {
                "kind": second_line_media["kind"],
                "label": second_line_media["kind"].replace("_", " ").title(),
                "filename": second_line_media["filename"],
                "display_name": second_line_media["display_name"],
                "size_text": second_line_media["size_text"],
                "asset": asset,
                "missing": asset is None,
                "caption": caption,
            }

    return None


def enrich_messages_with_media(messages: List[Dict], media_index: Dict[str, Dict]) -> List[Dict]:
    for item in messages:
        if item.get("type") != "message" or item.get("is_system"):
            continue

        media = extract_media_reference(item.get("message") or "", media_index)
        if media:
            item["media"] = media
            item["message"] = media["caption"]

    return messages


def data_uri_from_asset(asset: Dict) -> str:
    encoded = base64.b64encode(asset["bytes"]).decode("ascii")
    return f"data:{asset['content_type']};base64,{encoded}"


def build_export_label(render_mode: str) -> str:
    if render_mode == "html":
        return "Interactive HTML Export"
    if render_mode == "pdf_media":
        return "PDF + Media Package"
    return "PDF Only"


def render_media_block(msg: Dict, render_mode: str) -> str:
    media = msg.get("media")
    if not media:
        return ""

    asset = media.get("asset")
    kind = media.get("kind") or "attachment"
    filename = media.get("display_name") or media.get("filename") or "Attachment"
    size_text = media.get("size_text")
    meta_line = html.escape(f"{filename}" + (f" · {size_text}" if size_text else ""))

    if kind == "image":
        if asset:
            image_src = asset["relative_path"] if render_mode == "html" else data_uri_from_asset(asset)
            image_html = (
                f'<img src="{image_src}" alt="{html.escape(filename)}" class="chat-image" />'
            )
        else:
            image_html = '<div class="missing-media">Image not found</div>'

        details = ""
        if meta_line:
            details = f'<div class="attachment-meta">{meta_line}</div>'

        return f"""
        <div class="media-block">
          <div class="image-wrap">
            {image_html}
          </div>
          {details}
        </div>
        """

    if kind in {"voice", "audio"} and render_mode == "html" and asset:
        return f"""
        <div class="media-block">
          <div class="voice-card">
            <div class="voice-title">{'Voice note' if kind == 'voice' else 'Audio file'}</div>
            <audio controls preload="metadata" class="voice-player">
              <source src="{asset['relative_path']}" type="{html.escape(asset['content_type'])}">
            </audio>
            <div class="attachment-meta">{meta_line}</div>
          </div>
        </div>
        """

    title = {
        "voice": "Voice note attached",
        "audio": "Audio attached",
        "video": "Video attached",
        "document": "Document attached",
        "sticker": "Sticker attached",
        "gif": "GIF attached",
        "attachment": "Attachment",
    }.get(kind, "Attachment")

    action_html = ""
    if render_mode == "html" and asset and kind not in {"voice", "audio"}:
        action_html = (
            f'<a class="attachment-link" href="{asset["relative_path"]}" target="_blank" rel="noopener">'
            f'Open file</a>'
        )
    elif render_mode == "pdf_media" and asset and kind in {"voice", "audio", "video", "document", "attachment"}:
        action_html = (
            f'<a class="attachment-link" href="{asset["relative_path"]}" target="_blank" rel="noopener">'
            f'Open externally</a>'
        )

    missing_note = ""
    if media.get("missing"):
        missing_note = '<div class="attachment-note">Referenced file not found in export ZIP.</div>'

    return f"""
    <div class="media-block">
      <div class="attachment-card">
        <div class="attachment-title">{html.escape(title)}</div>
        <div class="attachment-meta">{meta_line}</div>
        {missing_note}
        {action_html}
      </div>
    </div>
    """


def build_chat_html(source_name: str, items: List[Dict], chunk_no: int, total_chunks: int, render_mode: str) -> str:
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
        media_html = render_media_block(msg, render_mode)
        text_html = (
            f'<div class="message-text">{format_message_html(msg["message"])}</div>'
            if (msg.get("message") or "").strip()
            else ""
        )

        chunks.append(
            f"""
            <div class="msg-row {side_class}">
              <div class="bubble {side_class}">
                {sender_html}
                {media_html}
                {text_html}
                <div class="meta">{html.escape(msg["time"] or "")}</div>
              </div>
            </div>
            """
        )

    chat_body = "\n".join(chunks)
    export_label = build_export_label(render_mode)

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
      max-width: 76%;
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
      margin-top: 4px;
    }}

    .media-block {{
      margin-top: 6px;
    }}

    .image-wrap {{
      background: rgba(255,255,255,0.55);
      border-radius: 10px;
      overflow: hidden;
      border: 1px solid rgba(15, 23, 42, 0.08);
    }}

    .chat-image {{
      display: block;
      width: 100%;
      max-width: 100%;
      height: auto;
      max-height: 220mm;
      object-fit: contain;
      background: #f8fafc;
    }}

    .voice-card,
    .attachment-card {{
      background: rgba(255,255,255,0.55);
      border: 1px solid rgba(15, 23, 42, 0.08);
      border-radius: 10px;
      padding: 10px;
    }}

    .voice-title,
    .attachment-title {{
      font-size: 12px;
      font-weight: 700;
      color: #111827;
      margin-bottom: 6px;
    }}

    .voice-player {{
      width: 100%;
      max-width: 100%;
    }}

    .attachment-meta {{
      font-size: 11px;
      color: #475569;
      margin-top: 6px;
      line-height: 1.5;
    }}

    .attachment-note {{
      font-size: 11px;
      color: #b45309;
      margin-top: 6px;
    }}

    .attachment-link {{
      display: inline-block;
      margin-top: 8px;
      font-size: 11px;
      color: #1d4ed8;
      text-decoration: none;
      font-weight: 600;
    }}

    .attachment-link:hover {{
      text-decoration: underline;
    }}

    .missing-media {{
      padding: 18px 14px;
      text-align: center;
      background: #fef2f2;
      color: #b91c1c;
      font-size: 12px;
      font-weight: 700;
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
      <div>
        <strong>{html.escape(source_name)}</strong><br />
        <span>{html.escape(export_label)}</span>
      </div>
      <div>Part {chunk_no} of {total_chunks}</div>
    </div>

    {chat_body}
  </div>
</body>
</html>
    """


async def render_chunk_pdf(
    browser,
    source_name: str,
    chunk: List[Dict],
    chunk_no: int,
    total_chunks: int,
    render_mode: str,
) -> bytes:
    html_content = build_chat_html(source_name, chunk, chunk_no, total_chunks, render_mode)
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


async def build_pdf_bytes(source_name: str, items: List[Dict], render_mode: str) -> bytes:
    has_media = any(item.get("media") for item in items if item.get("type") == "message")
    chunk_size = 35 if has_media else 80
    chunks = chunk_items(items, chunk_size=chunk_size)
    writer = PdfWriter()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            total_chunks = len(chunks)
            for idx, chunk in enumerate(chunks, start=1):
                pdf_bytes = await render_chunk_pdf(
                    browser=browser,
                    source_name=source_name,
                    chunk=chunk,
                    chunk_no=idx,
                    total_chunks=total_chunks,
                    render_mode=render_mode,
                )
                reader = PdfReader(BytesIO(pdf_bytes))
                for page in reader.pages:
                    writer.add_page(page)
        finally:
            await browser.close()

    output_buffer = BytesIO()
    writer.write(output_buffer)
    return output_buffer.getvalue()


def build_html_zip_bytes(source_name: str, items: List[Dict], media_index: Dict[str, Dict]) -> bytes:
    html_content = build_chat_html(
        source_name=source_name,
        items=items,
        chunk_no=1,
        total_chunks=1,
        render_mode="html",
    )

    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", html_content)
        for asset in media_index.values():
            zf.writestr(f"media/{asset['output_name']}", asset["bytes"])

    return output.getvalue()


def build_pdf_media_zip_bytes(source_name: str, pdf_bytes: bytes, media_index: Dict[str, Dict]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{source_name}.pdf", pdf_bytes)
        for asset in media_index.values():
            zf.writestr(f"media/{asset['output_name']}", asset["bytes"])

    return output.getvalue()


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


def export_response(file_bytes: bytes, filename: str, media_type: str):
    return StreamingResponse(
        BytesIO(file_bytes),
        media_type=media_type,
        headers={"Content-Disposition": build_download_header(filename)},
    )


async def generate_export(request: Request, file: UploadFile, export_format: str):
    if not SUPABASE_URL or not SUPABASE_ANON_KEY or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="Backend pricing config is missing.")

    if export_format not in EXPORT_FORMATS:
        raise HTTPException(status_code=400, detail="Invalid export format.")

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

    source_name, text, media_index = extract_chat_bundle(filename, content)
    items = enrich_messages_with_media(parse_whatsapp_text(text), media_index)

    if not items:
        raise HTTPException(status_code=400, detail="Could not parse any messages from this file.")

    if export_format == "html_zip":
        export_bytes = build_html_zip_bytes(source_name, items, media_index)
        await record_usage_event(user["id"])
        return export_response(
            file_bytes=export_bytes,
            filename=f"{source_name}-interactive-html.zip",
            media_type="application/zip",
        )

    if export_format == "pdf_media_zip":
        pdf_bytes = await build_pdf_bytes(source_name, items, render_mode="pdf_media")
        export_bytes = build_pdf_media_zip_bytes(source_name, pdf_bytes, media_index)
        await record_usage_event(user["id"])
        return export_response(
            file_bytes=export_bytes,
            filename=f"{source_name}-pdf-media.zip",
            media_type="application/zip",
        )

    pdf_bytes = await build_pdf_bytes(source_name, items, render_mode="pdf")
    await record_usage_event(user["id"])
    return export_response(
        file_bytes=pdf_bytes,
        filename=f"{source_name}.pdf",
        media_type="application/pdf",
    )


@app.post("/export-chat")
async def export_chat(
    request: Request,
    file: UploadFile = File(...),
    export_format: str = Form("pdf"),
):
    try:
        return await generate_export(request, file, export_format)
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse(status_code=500, content={"detail": f"Unexpected server error: {str(exc)}"})


@app.post("/convert-txt")
async def convert_txt(request: Request, file: UploadFile = File(...)):
    try:
        return await generate_export(request, file, "pdf")
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse(status_code=500, content={"detail": f"Unexpected server error: {str(exc)}"})
