import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import fitz
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = None

BASE_DIR = Path(tempfile.gettempdir()) / "pdf_obsidian_converter"
UPLOAD_DIR = BASE_DIR / "uploads"
RESULT_DIR = BASE_DIR / "results"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
RESULT_DIR.mkdir(parents=True, exist_ok=True)

jobs = {}
jobs_lock = threading.Lock()


@dataclass
class FlowItem:
    kind: str
    y0: float
    content: str
    page: int
    level: int = 0
    x0: float = 0
    y1: float = 0
    break_before: bool = False


def update_job(job_id, **updates):
    with jobs_lock:
        current = jobs.setdefault(job_id, {})
        current.update(updates)
        current["updated_at"] = time.time()


def get_job(job_id):
    with jobs_lock:
        return dict(jobs.get(job_id, {}))


def slugify_filename(name):
    stem = Path(name).stem or "book"
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "", stem).strip()
    stem = re.sub(r"\s+", "_", stem)
    return stem[:90] or "book"


def is_bold_span(span):
    font = span.get("font", "").lower()
    flags = int(span.get("flags", 0))
    return bool(flags & 16) or any(token in font for token in ["bold", "black", "heavy", "semibold", "demi"])


def normalize_text(text):
    return re.sub(r"\s+", " ", text).strip()


def clean_markdown_spacing(text):
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([(\[{])\s+", r"\1", text)
    text = re.sub(r"\s+([)\]}])", r"\1", text)
    return normalize_text(text)


def markdown_for_spans(spans):
    pieces = []
    for span in spans:
        text = normalize_text(span.get("text", ""))
        if not text:
            continue
        if is_bold_span(span):
            text = f"***{text}***"
        pieces.append(text)
    return clean_markdown_spacing(" ".join(pieces))


def normalize_list_marker(text):
    text = text.strip()
    bullet_match = re.match(r"^[•●◦▪▫‣∙·]\s*(.+)$", text)
    if bullet_match:
        return f"- {bullet_match.group(1).strip()}"
    number_match = re.match(r"^(\d+)[.)]\s+(.+)$", text)
    if number_match:
        return f"{number_match.group(1)}. {number_match.group(2).strip()}"
    return text


def is_standalone_bullet_marker(text):
    return text.strip() in {"•", "●", "◦", "▪", "▫", "‣", "∙", "·", ".", "•·"}


def is_markdown_list_item(text):
    return bool(re.match(r"^\s*(?:[-*+]\s+|\d+\.\s+)", text))


def has_list_like_neighbor(items, index, base_x0):
    item = items[index]
    if item.x0 - base_x0 < 12:
        return False
    for neighbor_index in (index - 1, index + 1):
        if neighbor_index < 0 or neighbor_index >= len(items):
            continue
        neighbor = items[neighbor_index]
        if neighbor.kind != "text" or neighbor.level or is_standalone_bullet_marker(neighbor.content):
            continue
        if abs(neighbor.x0 - item.x0) <= 5 and abs(neighbor.y0 - item.y0) < 80:
            return True
        if is_markdown_list_item(neighbor.content) and abs(neighbor.y0 - item.y0) < 80:
            return True
    return False


def collect_font_profile(doc):
    sizes = {}
    for page in doc:
        blocks = page.get_text("dict").get("blocks", [])
        for block in blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = normalize_text(span.get("text", ""))
                    if text:
                        size = round(float(span.get("size", 0)), 1)
                        sizes[size] = sizes.get(size, 0) + len(text)
    if not sizes:
        return 11.0
    return max(sizes.items(), key=lambda item: item[1])[0]


def classify_line(text, size, bold, body_size):
    if not text:
        return 0
    compact = len(text) <= 110
    words = len(text.split())
    if size >= body_size * 1.65 and compact:
        return 1
    if size >= body_size * 1.35 and compact:
        return 2
    if (size >= body_size * 1.18 and bold and compact) or (bold and words <= 10 and size >= body_size * 1.08):
        return 3
    return 0


def extract_text_items(page, page_number, body_size):
    items = []
    blocks = page.get_text("dict").get("blocks", [])
    for block in blocks:
        if block.get("type") != 0:
            continue
        previous_body_line = None
        for line in block.get("lines", []):
            spans = [span for span in line.get("spans", []) if normalize_text(span.get("text", ""))]
            if not spans:
                continue
            text = normalize_text(" ".join(span.get("text", "") for span in spans))
            bbox = line.get("bbox") or block.get("bbox") or [0, 0, 0, 0]
            max_size = max(float(span.get("size", body_size)) for span in spans)
            bold = any(is_bold_span(span) for span in spans)
            level = classify_line(text, max_size, bold, body_size)
            content = text if level else markdown_for_spans(spans)
            if not level:
                content = normalize_list_marker(content)
            x0 = float(bbox[0])
            y0 = float(bbox[1])
            y1 = float(bbox[3])
            break_before = False
            if level:
                previous_body_line = None
            else:
                if previous_body_line is None:
                    break_before = True
                else:
                    prev_x0, prev_y1, prev_height = previous_body_line
                    gap = y0 - prev_y1
                    indentation_shift = x0 - prev_x0
                    if gap > max(prev_height * 0.75, body_size * 0.9) or indentation_shift > max(body_size * 1.5, 18):
                        break_before = True
                previous_body_line = (x0, y1, max(y1 - y0, body_size))
            items.append(FlowItem("text", y0, content, page_number, level, x0, y1, break_before))
    return items


def image_dpi_for_rect(image_width, image_height, rect):
    width_inches = max(rect.width / 72, 0.01)
    height_inches = max(rect.height / 72, 0.01)
    return min(image_width / width_inches, image_height / height_inches)


def extension_for_image(info):
    ext = (info.get("ext") or "png").lower()
    if ext in {"jpeg", "jpg"}:
        return "jpg"
    if ext in {"png", "webp", "tiff", "bmp"}:
        return ext
    return "png"


def extract_image_file(doc, page, xref, rect, attachments_dir, image_index):
    info = doc.extract_image(xref)
    image_bytes = info.get("image")
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    ext = extension_for_image(info)
    dpi = image_dpi_for_rect(width, height, rect) if width and height else 0
    name = f"image_{image_index:04d}.{'png' if dpi < 300 else ext}"
    output_path = attachments_dir / name
    if image_bytes and dpi >= 300:
        output_path.write_bytes(image_bytes)
    else:
        matrix = fitz.Matrix(300 / 72, 300 / 72)
        pix = page.get_pixmap(matrix=matrix, clip=rect, alpha=False)
        pix.save(output_path)
    return name


def extract_image_items(doc, page, page_number, attachments_dir, start_index):
    items = []
    image_index = start_index
    seen = set()
    for image in page.get_images(full=True):
        xref = image[0]
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            rects = []
        for rect in rects:
            key = (xref, round(rect.x0, 2), round(rect.y0, 2), round(rect.x1, 2), round(rect.y1, 2))
            if key in seen or rect.is_empty or rect.width < 3 or rect.height < 3:
                continue
            seen.add(key)
            try:
                name = extract_image_file(doc, page, xref, rect, attachments_dir, image_index)
                items.append(FlowItem("image", float(rect.y0), name, page_number, x0=float(rect.x0), y1=float(rect.y1)))
                image_index += 1
            except Exception:
                continue
    return items, image_index


def markdown_for_items(items, link_style):
    markdown = []
    paragraph = []
    pending_bullet_marker = False
    body_x_positions = [
        item.x0
        for item in items
        if item.kind == "text" and not item.level and not is_standalone_bullet_marker(item.content) and len(item.content) > 3
    ]
    base_x0 = min(body_x_positions) if body_x_positions else 0

    def flush_paragraph():
        if paragraph:
            if any(is_markdown_list_item(line) for line in paragraph):
                markdown.append("\n".join(paragraph).strip())
            else:
                markdown.append("  \n".join(paragraph).strip())
            paragraph.clear()

    for index, item in enumerate(items):
        if item.kind == "image":
            flush_paragraph()
            target = f"attachments/{item.content}" if link_style == "folder" else item.content
            markdown.append(f"![[{target}]]")
            continue
        if item.level:
            flush_paragraph()
            markdown.append(f"{'#' * item.level} {item.content}")
        else:
            if is_standalone_bullet_marker(item.content):
                if paragraph and not is_markdown_list_item(paragraph[-1]):
                    flush_paragraph()
                pending_bullet_marker = True
                continue
            if pending_bullet_marker:
                item.content = f"- {item.content.lstrip('-*+ ').strip()}"
                item.break_before = True
                pending_bullet_marker = False
            elif (
                item.break_before
                and not is_markdown_list_item(item.content)
                and has_list_like_neighbor(items, index, base_x0)
            ):
                item.content = f"- {item.content}"
            current_is_list_item = is_markdown_list_item(item.content)
            previous_is_list_item = bool(paragraph and is_markdown_list_item(paragraph[-1]))
            if item.break_before and not (current_is_list_item and previous_is_list_item):
                flush_paragraph()
            paragraph.append(item.content)
    flush_paragraph()
    return "\n\n".join(section for section in markdown if section)


def assemble_chunks(job_id, filename, total_chunks):
    job_dir = UPLOAD_DIR / job_id
    chunk_dir = job_dir / "chunks"
    output_path = job_dir / secure_filename(filename or "uploaded.pdf")
    with output_path.open("wb") as output_file:
        for index in range(total_chunks):
            chunk_path = chunk_dir / f"{index}.part"
            if not chunk_path.exists():
                raise FileNotFoundError(f"Missing upload chunk {index + 1} of {total_chunks}")
            with chunk_path.open("rb") as chunk_file:
                shutil.copyfileobj(chunk_file, output_file, length=1024 * 1024)
    return output_path


def process_pdf_job(job_id, filename, total_chunks, link_style):
    try:
        update_job(job_id, status="processing", progress=8, message="Assembling uploaded chunks...")
        input_path = assemble_chunks(job_id, filename, int(total_chunks))
        book_name = slugify_filename(filename)
        output_dir = RESULT_DIR / job_id / book_name
        attachments_dir = output_dir / "attachments"
        if output_dir.exists():
            shutil.rmtree(output_dir)
        attachments_dir.mkdir(parents=True, exist_ok=True)

        update_job(job_id, progress=15, message="Reading PDF structure...")
        doc = fitz.open(input_path)
        body_size = collect_font_profile(doc)
        page_markdown_sections = []
        total_chars = 0
        image_index = 1
        page_count = max(len(doc), 1)

        for page_number, page in enumerate(doc, start=1):
            update_job(job_id, progress=15 + int((page_number - 1) / page_count * 60), message=f"Reading text and images on page {page_number} of {page_count}...")
            flow_items = []
            text_items = extract_text_items(page, page_number, body_size)
            image_items, image_index = extract_image_items(doc, page, page_number, attachments_dir, image_index)
            flow_items.extend(text_items)
            flow_items.extend(image_items)
            flow_items.sort(key=lambda item: (item.y0, item.x0, 0 if item.kind == "text" else 1))
            page_markdown = markdown_for_items(flow_items, link_style)
            page_markdown_sections.append(page_markdown)
            total_chars += len(page_markdown)

        update_job(job_id, progress=80, message="Formatting Obsidian Markdown...")
        note_parts = []
        if total_chars < max(40, page_count * 10):
            note_parts.append("> Note: This PDF appears to contain little extractable text. It may be a scanned book, so OCR may be required for complete text extraction. Images were still extracted where possible.")
        note_parts.append(f"# {book_name.replace('_', ' ')}")
        book_markdown = "\n\n---\n\n".join(page_markdown_sections).strip()
        note_parts.append(book_markdown or "No extractable text was found in this PDF.")
        md_path = output_dir / f"{book_name}.md"
        md_path.write_text("\n\n".join(note_parts), encoding="utf-8")

        update_job(job_id, progress=90, message="Zipping files...")
        archive_base = RESULT_DIR / job_id / book_name
        zip_path = shutil.make_archive(str(archive_base), "zip", root_dir=output_dir)
        doc.close()
        update_job(job_id, status="complete", progress=100, message="Done. Your Obsidian ZIP is ready.", download_url=f"/download/{job_id}", zip_path=zip_path)
    except Exception as exc:
        update_job(job_id, status="error", progress=100, message=str(exc))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload-chunk", methods=["POST"])
def upload_chunk():
    job_id = request.form.get("job_id") or str(uuid.uuid4())
    chunk_index = request.form.get("chunk_index", type=int)
    total_chunks = request.form.get("total_chunks", type=int)
    filename = request.form.get("filename", "uploaded.pdf")
    chunk = request.files.get("chunk")
    if chunk is None or chunk_index is None or total_chunks is None:
        return jsonify({"error": "Invalid chunk upload"}), 400
    if not filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a PDF file"}), 400
    chunk_dir = UPLOAD_DIR / job_id / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunk.save(chunk_dir / f"{chunk_index}.part")
    update_job(job_id, status="uploading", progress=int((chunk_index + 1) / total_chunks * 8), message=f"Uploading chunk {chunk_index + 1} of {total_chunks}...", filename=filename, total_chunks=total_chunks)
    return jsonify({"job_id": job_id, "uploaded": chunk_index + 1, "total": total_chunks})


@app.route("/process", methods=["POST"])
def process():
    data = request.get_json(force=True)
    job_id = data.get("job_id")
    filename = data.get("filename", "uploaded.pdf")
    total_chunks = int(data.get("total_chunks", 0))
    link_style = data.get("link_style", "folder")
    if not job_id or total_chunks < 1:
        return jsonify({"error": "Upload is incomplete"}), 400
    thread = threading.Thread(target=process_pdf_job, args=(job_id, filename, total_chunks, link_style), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"status": "missing", "progress": 0, "message": "Job not found"}), 404
    return jsonify({key: value for key, value in job.items() if key != "zip_path"})


@app.route("/download/<job_id>")
def download(job_id):
    job = get_job(job_id)
    zip_path = job.get("zip_path")
    if not zip_path or not Path(zip_path).exists():
        return jsonify({"error": "Result not found"}), 404
    return send_file(zip_path, as_attachment=True, download_name=Path(zip_path).name, mimetype="application/zip")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
