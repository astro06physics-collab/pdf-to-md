# Workspace

## Overview

Python Flask web application for converting uploaded PDF books into Obsidian-ready Markdown ZIP files. The app supports chunked browser uploads for large PDFs, extracts text and images with PyMuPDF, identifies headings from font metadata, and packages results as `book_name.md` plus an `attachments/` folder.

## Stack

- **Language**: Python
- **Web framework**: Flask
- **PDF processing**: PyMuPDF (`fitz`)
- **Packaging**: `shutil.make_archive`
- **Frontend**: HTML, CSS, JavaScript with chunked upload and progress polling

## Key Commands

- `python app.py` — run the Flask app locally

## Application Behavior

- Uploads PDFs in 5 MB chunks to avoid single-request memory pressure.
- Processes pages sequentially and extracts image placements into the Markdown flow.
- Preserves Markdown paragraph and line breaks using PDF text blocks, spacing gaps, indentation changes, and hard line breaks.
- Converts common PDF bullet glyphs into Markdown `- ` list items.
- Inserts `---` horizontal rules between PDF pages in the generated Markdown.
- Uses Obsidian image embeds with either `![[attachments/image.png]]` or `![[image.png]]`.
- Adds an OCR note when a PDF appears scanned or has very little extractable text.

## Project Notes

Temporary uploads and generated ZIPs are stored under the system temp directory at runtime.
