from io import BytesIO
import re
from typing import List, Dict, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, white
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


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

# Common WhatsApp export patterns
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
    """
    WhatsApp lines may look like:
    John Doe: Hello
    Messages to this group are now secured...
    """
    if ": " in rest:
        sender, message = rest.split(": ", 1)
        return sender.strip(), message.strip(), False
    return None, rest.strip(), True


def parse_whatsapp_text(text: str) -> List[Dict]:
    messages: List[Dict] = []
    sender_side_map: Dict[str, str] = {}

    lines = text.splitlines()

    for raw_line in lines:
        line = raw_line.rstrip()

        if not line.strip():
            # keep paragraph spacing by appending blank line to previous message
            if messages:
                messages[-1]["message"] += "\n"
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

            messages.append(
                {
                    "date": date,
                    "time": time,
                    "sender": sender,
                    "message": message,
                    "is_system": is_system,
                    "side": side,
                }
            )
        else:
            # continuation of previous message
            if messages:
                if messages[-1]["message"]:
                    messages[-1]["message"] += "\n" + line
                else:
                    messages[-1]["message"] = line
            else:
                messages.append(
                    {
                        "date": "",
                        "time": "",
                        "sender": None,
                        "message": line,
                        "is_system": True,
                        "side": "center",
                    }
                )

    return messages


def wrap_text(text: str, max_width: float, font_name: str, font_size: int) -> List[str]:
    """
    Wraps text while preserving paragraphs.
    """
    if not text:
        return [""]

    paragraphs = text.split("\n")
    final_lines: List[str] = []

    for para in paragraphs:
        para = para.strip()
        if para == "":
            final_lines.append("")
            continue

        words = para.split()
        if not words:
            final_lines.append("")
            continue

        current = words[0]
        for word in words[1:]:
            test = f"{current} {word}"
            if stringWidth(test, font_name, font_size) <= max_width:
                current = test
            else:
                final_lines.append(current)
                current = word
        final_lines.append(current)

    return final_lines


def draw_page_background(pdf: canvas.Canvas, width: float, height: float, page_num: int):
    # page background
    pdf.setFillColor(HexColor("#efeae2"))
    pdf.rect(0, 0, width, height, fill=1, stroke=0)

    # top bar
    pdf.setFillColor(HexColor("#0f8f7d"))
    pdf.rect(0, height - 42, width, 42, fill=1, stroke=0)

    pdf.setFillColor(white)
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(24, height - 27, "WAChatPrint")

    # footer
    pdf.setFillColor(HexColor("#6b7280"))
    pdf.setFont("Helvetica", 9)
    footer_text = f"Page {page_num}"
    pdf.drawRightString(width - 24, 16, footer_text)


def draw_header(pdf: canvas.Canvas, width: float, y_top: float, source_name: str, first_date: str, last_date: str):
    y = y_top

    pdf.setFillColor(HexColor("#ffffff"))
    pdf.roundRect(24, y - 42, width - 48, 36, 10, fill=1, stroke=0)

    pdf.setFillColor(HexColor("#111827"))
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(36, y - 22, source_name)

    pdf.setFont("Helvetica", 9)
    pdf.setFillColor(HexColor("#6b7280"))
    date_range = f"{first_date}  →  {last_date}" if first_date or last_date else "Imported chat"
    pdf.drawRightString(width - 36, y - 22, date_range)

    return y - 54


def message_bubble_height(lines: List[str], include_sender: bool) -> float:
    top_pad = 8
    bottom_pad = 8
    sender_h = 12 if include_sender else 0
    line_h = 12 * max(1, len(lines))
    time_h = 11
    return top_pad + sender_h + line_h + time_h + bottom_pad


def draw_message_bubble(
    pdf: canvas.Canvas,
    width: float,
    y_top: float,
    msg: Dict,
):
    page_margin = 24
    bubble_max_width = 290
    text_font = "Helvetica"
    text_size = 10
    sender_font = "Helvetica-Bold"
    sender_size = 9
    time_font = "Helvetica"
    time_size = 8
    padding_x = 10
    padding_y = 8

    if msg["is_system"]:
        box_width = width - 160
        x = (width - box_width) / 2
        lines = wrap_text(msg["message"], box_width - (padding_x * 2), text_font, text_size)
        height_needed = message_bubble_height(lines, include_sender=False)

        y_bottom = y_top - height_needed

        pdf.setFillColor(HexColor("#d1d5db"))
        pdf.roundRect(x, y_bottom, box_width, height_needed, 8, fill=1, stroke=0)

        cursor_y = y_top - padding_y - 10
        pdf.setFillColor(HexColor("#374151"))
        pdf.setFont(text_font, text_size)
        for line in lines:
            pdf.drawCentredString(width / 2, cursor_y, line if line else " ")
            cursor_y -= 12

        if msg["time"]:
            pdf.setFont(time_font, time_size)
            pdf.setFillColor(HexColor("#6b7280"))
            pdf.drawCentredString(width / 2, y_bottom + 6, msg["time"])

        return y_bottom - 8

    side = msg["side"]
    bubble_width = bubble_max_width

    lines = wrap_text(msg["message"], bubble_width - (padding_x * 2), text_font, text_size)
    include_sender = bool(msg["sender"])
    height_needed = message_bubble_height(lines, include_sender=include_sender)

    x = page_margin if side == "left" else width - page_margin - bubble_width
    y_bottom = y_top - height_needed

    fill_color = HexColor("#ffffff") if side == "left" else HexColor("#dcf8c6")
    pdf.setFillColor(fill_color)
    pdf.roundRect(x, y_bottom, bubble_width, height_needed, 10, fill=1, stroke=0)

    cursor_y = y_top - padding_y - 9

    if include_sender:
        sender_color = HexColor("#2563eb") if side == "left" else HexColor("#0f8f7d")
        pdf.setFillColor(sender_color)
        pdf.setFont(sender_font, sender_size)
        pdf.drawString(x + padding_x, cursor_y, msg["sender"][:36])
        cursor_y -= 12

    pdf.setFillColor(HexColor("#111827"))
    pdf.setFont(text_font, text_size)
    for line in lines:
        pdf.drawString(x + padding_x, cursor_y, line if line else " ")
        cursor_y -= 12

    if msg["time"]:
        pdf.setFont(time_font, time_size)
        pdf.setFillColor(HexColor("#6b7280"))
        pdf.drawRightString(x + bubble_width - padding_x, y_bottom + 6, msg["time"])

    return y_bottom - 10


@app.post("/convert-txt")
async def convert_txt(file: UploadFile = File(...)):
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

    messages = parse_whatsapp_text(text)

    if not messages:
        raise HTTPException(status_code=400, detail="Could not parse any messages from this file.")

    source_name = filename.rsplit(".", 1)[0]
    first_date = messages[0]["date"] if messages else ""
    last_date = messages[-1]["date"] if messages else ""

    pdf_buffer = BytesIO()
    pdf = canvas.Canvas(pdf_buffer, pagesize=A4)
    width, height = A4

    page_num = 1
    draw_page_background(pdf, width, height, page_num)
    y = draw_header(pdf, width, height - 12, source_name, first_date, last_date)

    for msg in messages:
        bubble_preview_lines = wrap_text(msg["message"], 270, "Helvetica", 10)
        need_height = message_bubble_height(bubble_preview_lines, include_sender=bool(msg["sender"])) + 14

        if y - need_height < 30:
            pdf.showPage()
            page_num += 1
            draw_page_background(pdf, width, height, page_num)
            y = draw_header(pdf, width, height - 12, source_name, first_date, last_date)

        y = draw_message_bubble(pdf, width, y, msg)

    pdf.save()
    pdf_buffer.seek(0)

    output_name = filename.rsplit(".", 1)[0] + ".pdf"

    return StreamingResponse(
        pdf_buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{output_name}"'}
    )
