from io import BytesIO
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from reportlab.lib.pagesizes import A4
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


@app.get("/")
def root():
    return {"message": "WAChatPrint API is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


def wrap_text(text: str, max_width: float, font_name: str, font_size: int):
    words = text.split()
    if not words:
        return [""]

    lines = []
    current_line = words[0]

    for word in words[1:]:
        test_line = f"{current_line} {word}"
        if stringWidth(test_line, font_name, font_size) <= max_width:
            current_line = test_line
        else:
            lines.append(current_line)
            current_line = word

    lines.append(current_line)
    return lines


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

    pdf_buffer = BytesIO()
    pdf = canvas.Canvas(pdf_buffer, pagesize=A4)

    width, height = A4
    left_margin = 40
    right_margin = 40
    top_margin = 50
    bottom_margin = 40
    line_height = 14
    usable_width = width - left_margin - right_margin

    y = height - top_margin

    pdf.setTitle("WAChatPrint Export")
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(left_margin, y, "WAChatPrint")
    y -= 24

    pdf.setFont("Helvetica", 10)
    pdf.drawString(left_margin, y, f"Source file: {filename}")
    y -= 24

    pdf.setFont("Helvetica", 10)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            y -= line_height
        else:
            wrapped_lines = wrap_text(line, usable_width, "Helvetica", 10)
            for wrapped in wrapped_lines:
                if y <= bottom_margin:
                    pdf.showPage()
                    pdf.setFont("Helvetica", 10)
                    y = height - top_margin
                pdf.drawString(left_margin, y, wrapped)
                y -= line_height

        if y <= bottom_margin:
            pdf.showPage()
            pdf.setFont("Helvetica", 10)
            y = height - top_margin

    pdf.save()
    pdf_buffer.seek(0)

    output_name = filename.rsplit(".", 1)[0] + ".pdf"

    return StreamingResponse(
        pdf_buffer,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{output_name}"'
        },
    )
