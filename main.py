from io import BytesIO
import os
import re
from typing import List, Dict

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, white
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
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

# ---------- Font setup ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(BASE_DIR, "fonts")

TEXT_FONT_PATH = os.path.join(FONT_DIR, "DejaVuSans.ttf")
BOLD_FONT_PATH = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
EMOJI_FONT_PATH = os.path.join(FONT_DIR, "Symbola.ttf")

REQUIRED_FONTS = [TEXT_FONT_PATH, BOLD_FONT_PATH, EMOJI_FONT_PATH]
for font_path in REQUIRED_FONTS:
    if not os.path.exists(font_path):
        raise RuntimeError(f"Missing required font file: {font_path}")

TEXT_FONT = "ChatText"
BOLD_FONT = "ChatBold"
EMOJI_FONT = "ChatEmoji"

pdfmetrics.registerFont(TTFont(TEXT_FONT, TEXT_FONT_PATH))
pdfmetrics.registerFont(TTFont(BOLD_FONT, BOLD_FONT_PATH))
pdfmetrics.registerFont(TTFont(EMOJI_FONT, EMOJI_FONT_PATH))


# ---------- WhatsApp patterns ----------
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


def is_emoji_char(ch: str) -> bool:
    if not ch:
        return False

    code = ord(ch)

    emoji_ranges = [
        (0x1F300, 0x1F5FF),  # Misc Symbols and Pictographs
        (0x1F600, 0x1F64F),  # Emoticons
        (0x1F680, 0x1F6FF),  # Transport and Map
        (0x1F700, 0x1F77F),  # Alchemical Symbols
        (0x1F780, 0x1F7FF),  # Geometric Shapes Extended
        (0x1F800, 0x1F8FF),  # Supplemental Arrows-C
        (0x1F900, 0x1F9FF),  # Supplemental Symbols and Pictographs
        (0x1FA00, 0x1FA6F),  # Chess / symbols
        (0x1FA70, 0x1FAFF),  # Symbols and Pictographs Extended-A
        (0x2600, 0x26FF),    # Misc symbols
        (0x2700, 0x27BF),    # Dingbats
        (0x1F1E6, 0x1F1FF),  # Regional indicator flags
        (0xFE00, 0xFE0F),    # Variation selectors
        (0x200D, 0x200D),    # Zero-width joiner
    ]

    return any(start <= code <= end for start, end in emoji_ranges)


def font_for_char(ch: str, base_font: str) -> str:
    return EMOJI_FONT if is_emoji_char(ch) else base_font


def mixed_text_width(text: str, base_font: str, font_size: int) -> float:
    total = 0.0
    for ch in text:
        active_font = font_for_char(ch, base_font)
        try:
            total += pdfmetrics.stringWidth(ch, active_font, font_size)
        except Exception:
            total += pdfmetrics.stringWidth("?", base_font, font_size)
    return total


def draw_mixed_text(pdf: canvas.Canvas, x: float, y: float, text: str, base_font: str, font_size: int):
    if not text:
        return

    current_font = font_for_char(text[0], base_font)
    run = ""
    current_x = x

    for ch in text:
        active_font = font_for_char(ch, base_font)
        if active_font == current_font:
            run += ch
        else:
            pdf.setFont(current_font, font_size)
            pdf.drawString(current_x, y, run)
            current_x += pdfmetrics.stringWidth(run, current_font, font_size)
            run = ch
            current_font = active_font

    if run:
        pdf.setFont(current_font, font_size)
        pdf.drawString(current_x, y, run)


def draw_mixed_text_right(pdf: canvas.Canvas, right_x: float, y: float, text: str, base_font: str, font_size: int):
    width = mixed_text_width(text, base_font, font_size)
    draw_mixed_text(pdf, right_x - width, y, text, base_font, font_size)


def draw_mixed_text_center(pdf: canvas.Canvas, center_x: float, y: float, text: str, base_font: str, font_size: int):
    width = mixed_text_width(text, base_font, font_size)
    draw_mixed_text(pdf, center_x - (width / 2), y, text, base_font, font_size)


def split_sender_and_message(rest: str):
    if ": " in rest:
        sender, message = rest.split(": ", 1)
        return sender.strip(), message.strip(), False
    return None, rest.strip(), True


def parse_whatsapp_text(text: str) -> List[Dict]:
    messages: List[Dict] = []
    sender_side_map: Dict[str, str] = {}

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        if not line.strip():
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


def wrap_text(text: str, max_width: float, base_font: str, font_size: int) -> List[str]:
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

        current_line = words[0]

        for word in words[1:]:
            test_line = f"{current_line} {word}"
            if mixed_text_width(test_line, base_font, font_size) <= max_width:
                current_line = test_line
            else:
                final_lines.append(current_line)
                current_line = word

        final_lines.append(current_line)

    return final_lines


def draw_page_background(pdf: canvas.Canvas, width: float, height: float, page_num: int):
    pdf.setFillColor(HexColor("#efeae2"))
    pdf.rect(0, 0, width, height, fill=1, stroke=0)

    pdf.setFillColor(HexColor("#0f8f7d"))
    pdf.rect(0, height - 42, width, 42, fill=1, stroke=0)

    pdf.setFillColor(white)
    draw_mixed_text(pdf, 24, height - 27, "WAChatPrint", BOLD_FONT, 14)

    pdf.setFillColor(HexColor("#6b7280"))
    draw_mixed_text_right(pdf, width - 24, 16, f"Page {page_num}", TEXT_FONT, 9)


def draw_header(pdf: canvas.Canvas, width: float, y_top: float, source_name: str, first_date: str, last_date: str):
    y = y_top

    pdf.setFillColor(HexColor("#ffffff"))
    pdf.roundRect(24, y - 42, width - 48, 36, 10, fill=1, stroke=0)

    pdf.setFillColor(HexColor("#111827"))
    draw_mixed_text(pdf, 36, y - 22, source_name, BOLD_FONT, 12)

    pdf.setFillColor(HexColor("#6b7280"))
    date_range = f"{first_date}  →  {last_date}" if first_date or last_date else "Imported chat"
    draw_mixed_text_right(pdf, width - 36, y - 22, date_range, TEXT_FONT, 9)

    return y - 54


def message_bubble_height(lines: List[str], include_sender: bool) -> float:
    top_pad = 8
    bottom_pad = 8
    sender_h = 12 if include_sender else 0
    line_h = 12 * max(1, len(lines))
    time_h = 11
    return top_pad + sender_h + line_h + time_h + bottom_pad


def draw_message_bubble(pdf: canvas.Canvas, width: float, y_top: float, msg: Dict):
    page_margin = 24
    bubble_max_width = 290
    text_font = TEXT_FONT
    text_size = 10
    sender_font = BOLD_FONT
    sender_size = 9
    time_font = TEXT_FONT
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
        for line in lines:
            draw_mixed_text_center(pdf, width / 2, cursor_y, line if line else " ", text_font, text_size)
            cursor_y -= 12

        if msg["time"]:
            pdf.setFillColor(HexColor("#6b7280"))
            draw_mixed_text_center(pdf, width / 2, y_bottom + 6, msg["time"], time_font, time_size)

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
      draw_mixed_text(pdf, x + padding_x, cursor_y, msg["sender"][:36], sender_font, sender_size)
      cursor_y -= 12

    pdf.setFillColor(HexColor("#111827"))
    for line in lines:
        draw_mixed_text(pdf, x + padding_x, cursor_y, line if line else " ", text_font, text_size)
        cursor_y -= 12

    if msg["time"]:
        pdf.setFillColor(HexColor("#6b7280"))
        draw_mixed_text_right(pdf, x + bubble_width - padding_x, y_bottom + 6, msg["time"], time_font, time_size)

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
        preview_lines = wrap_text(msg["message"], 270, TEXT_FONT, 10)
        need_height = message_bubble_height(preview_lines, include_sender=bool(msg["sender"])) + 14

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
