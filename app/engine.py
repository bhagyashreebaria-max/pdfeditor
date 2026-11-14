"""
Prism Composable Document Engine
---------------------------------
High-performance, memory-first PDF manipulation engine designed around 4 core primitives:
1. Compose   (Merge, slice, reorder, rotate, blank, N-up layout, crop)
2. Transform (Compress, grayscale, format convert, rasterize, linearize)
3. Protect   (Encrypt, decrypt, sanitize, redact, digital signature)
4. Inspect   (Structure telemetry, metadata, font inventory, visual/text diff)
Plus the atomic Pipeline executor for chained single-pass operations.
"""

from __future__ import annotations

import difflib
from io import BytesIO
import json
import os
from pathlib import Path
import re
from typing import Any, Sequence
import zipfile

import pymupdf as fitz
import pikepdf
from PIL import Image, ImageOps
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign import signers
from pyhanko.sign.fields import MDPPerm, SigFieldSpec
from pyhanko.sign.general import SigningError
from pyhanko.sign.validation import validate_pdf_signature

MAX_FILE_BYTES = 20 * 1024 * 1024 * 1024  # 20 GiB
STANDARD_PAGE_SIZES = {
    "a4": (595.28, 841.89),
    "letter": (612.0, 792.0),
    "legal": (612.0, 1008.0),
    "a3": (841.89, 1190.55),
    "a5": (419.53, 595.28),
}


class EngineError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _open_pdf(data: bytes | str | Path, password: str | None = None) -> fitz.Document:
    try:
        if isinstance(data, (str, Path)):
            doc = fitz.open(str(data))
        else:
            doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise EngineError(f"Invalid PDF data: {exc}", status_code=400) from exc

    if doc.is_encrypted:
        if not password or not doc.authenticate(password):
            doc.close()
            raise EngineError("Document is password protected or incorrect password provided.", status_code=401)
    return doc


def parse_page_range(range_str: str, total_pages: int) -> list[int]:
    """Parse expressions like '1-3, 5, 8-10' into 0-indexed page indices."""
    if not range_str or range_str.strip().lower() in ("all", "*"):
        return list(range(total_pages))
    
    indices = []
    tokens = [t.strip() for t in range_str.split(",") if t.strip()]
    for token in tokens:
        if "-" in token:
            parts = token.split("-", 1)
            try:
                start = int(parts[0])
                end = int(parts[1])
            except ValueError as exc:
                raise EngineError(f"Invalid page range token '{token}'") from exc
            if start < 1 or end > total_pages or start > end:
                raise EngineError(f"Page range '{token}' out of bounds (1-{total_pages})")
            indices.extend(range(start - 1, end))
        else:
            try:
                page_num = int(token)
            except ValueError as exc:
                raise EngineError(f"Invalid page number '{token}'") from exc
            if page_num < 1 or page_num > total_pages:
                raise EngineError(f"Page number {page_num} out of bounds (1-{total_pages})")
            indices.append(page_num - 1)
    return indices


# ---------------------------------------------------------------------------
# 1. COMPOSE PRIMITIVE
# ---------------------------------------------------------------------------

def engine_compose(
    files: Sequence[tuple[str, bytes]],
    plan: dict[str, Any] | None = None,
) -> tuple[bytes, str, str]:
    """
    Unified page arrangement and multi-document composition.
    Options in plan:
      - page_selection: "1-3, 5" or list of 1-based page indices
      - rotation: int (90, 180, 270) or dict {page_idx: rotation}
      - reverse: bool
      - insert_blanks: list of 1-based page positions to insert blank pages after
      - crop_margins: [left, top, right, bottom] in points
      - target_size: "a4", "letter", "legal"
      - nup: 1, 2, or 4 (pages per sheet)
    """
    if not files:
        raise EngineError("At least one document is required for composition.")

    plan = plan or {}
    output_doc = fitz.open()

    # Step A: Load and merge input files
    for filename, data in files:
        if len(data) > MAX_FILE_BYTES:
            output_doc.close()
            raise EngineError(f"File '{filename}' exceeds {MAX_FILE_BYTES // (1024*1024)} MiB limit.", status_code=413)

        # Handle image input - visual A4 full-page scaling
        lower_name = filename.lower()
        if lower_name.endswith((".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff")):
            try:
                img = Image.open(BytesIO(data))
                img_w, img_h = img.size

                # Standard A4: 595.28 x 841.89 points
                # Select A4 Landscape if image is landscape, else A4 Portrait
                if img_w > img_h:
                    a4_w, a4_h = 841.89, 595.28  # A4 Landscape
                else:
                    a4_w, a4_h = 595.28, 841.89  # A4 Portrait

                page = output_doc.new_page(width=a4_w, height=a4_h)

                # Visually fill the full A4 page, keeping aspect ratio centered
                stretch = plan.get("image_stretch", False)
                page.insert_image(
                    fitz.Rect(0, 0, a4_w, a4_h),
                    stream=data,
                    keep_proportion=not stretch,
                )
            except Exception as exc:
                output_doc.close()
                raise EngineError(f"Could not convert image '{filename}' to A4 PDF: {exc}") from exc
            continue

        doc = _open_pdf(data)
        try:
            output_doc.insert_pdf(doc)
        finally:
            doc.close()

    total_pages = output_doc.page_count
    if total_pages == 0:
        output_doc.close()
        raise EngineError("No valid pages were provided in the input documents.")

    # Step B: Page Selection & Reordering
    page_sel = plan.get("page_selection")
    if page_sel:
        if isinstance(page_sel, str):
            selected_indices = parse_page_range(page_sel, total_pages)
        elif isinstance(page_sel, (list, tuple)):
            selected_indices = [int(p) - 1 for p in page_sel if 1 <= int(p) <= total_pages]
        else:
            selected_indices = list(range(total_pages))
        
        if not selected_indices:
            output_doc.close()
            raise EngineError("Page selection resulted in 0 pages.")
        
        new_doc = fitz.open()
        for idx in selected_indices:
            new_doc.insert_pdf(output_doc, from_page=idx, to_page=idx)
        output_doc.close()
        output_doc = new_doc
        total_pages = output_doc.page_count

    # Step C: Reverse order if requested
    if plan.get("reverse", False):
        rev_doc = fitz.open()
        for idx in reversed(range(total_pages)):
            rev_doc.insert_pdf(output_doc, from_page=idx, to_page=idx)
        output_doc.close()
        output_doc = rev_doc

    # Step D: Insert Blank Pages
    insert_blanks = plan.get("insert_blanks")
    if insert_blanks and isinstance(insert_blanks, (list, tuple)):
        # Sort positions descending to preserve offsets
        sorted_positions = sorted([int(p) for p in insert_blanks if isinstance(p, (int, str)) and str(p).isdigit()], reverse=True)
        for pos in sorted_positions:
            if 0 <= pos <= output_doc.page_count:
                # Insert blank page with dimensions of previous page
                ref_page = output_doc[max(0, min(pos - 1, output_doc.page_count - 1))]
                rect = ref_page.rect
                output_doc.new_page(pno=pos, width=rect.width, height=rect.height)

    # Step E: Apply Rotations
    rot_val = plan.get("rotation")
    if rot_val:
        if isinstance(rot_val, int):
            for page in output_doc:
                page.set_rotation((page.rotation + rot_val) % 360)
        elif isinstance(rot_val, dict):
            for page_str, angle in rot_val.items():
                p_idx = int(page_str) - 1
                if 0 <= p_idx < output_doc.page_count:
                    output_doc[p_idx].set_rotation((output_doc[p_idx].rotation + int(angle)) % 360)

    # Step F: Resize or Crop
    crop_margins = plan.get("crop_margins")
    if crop_margins and len(crop_margins) == 4:
        left, top, right, bottom = crop_margins
        for page in output_doc:
            r = page.rect
            page.set_cropbox(fitz.Rect(r.x0 + left, r.y0 + top, r.x1 - right, r.y1 - bottom))

    target_size = plan.get("target_size")
    if target_size and str(target_size).lower() in STANDARD_PAGE_SIZES:
        tw, th = STANDARD_PAGE_SIZES[str(target_size).lower()]
        resized_doc = fitz.open()
        for page in output_doc:
            new_page = resized_doc.new_page(width=tw, height=th)
            rect = fitz.Rect(0, 0, tw, th)
            new_page.show_pdf_page(rect, output_doc, page.number)
        output_doc.close()
        output_doc = resized_doc

    # Step G: N-up Layout (e.g. 2 pages per sheet)
    nup = plan.get("nup")
    if nup == 2 and output_doc.page_count >= 2:
        nup_doc = fitz.open()
        for i in range(0, output_doc.page_count, 2):
            sheet = nup_doc.new_page(width=841.89, height=595.28)  # A4 Landscape
            # Left page
            left_rect = fitz.Rect(0, 0, 420.94, 595.28)
            sheet.show_pdf_page(left_rect, output_doc, i)
            # Right page if exists
            if i + 1 < output_doc.page_count:
                right_rect = fitz.Rect(420.94, 0, 841.89, 595.28)
                sheet.show_pdf_page(right_rect, output_doc, i + 1)
        output_doc.close()
        output_doc = nup_doc

    out_bytes = output_doc.tobytes(garbage=3, deflate=True)
    output_doc.close()
    return out_bytes, "application/pdf", "composed.pdf"


# ---------------------------------------------------------------------------
# 2. TRANSFORM PRIMITIVE
# ---------------------------------------------------------------------------

def engine_transform(
    data: bytes,
    options: dict[str, Any] | None = None,
) -> tuple[bytes, str, str]:
    """
    High-efficiency conversions, compressions, and visual transformations.
    Options:
      - mode: "compress", "grayscale", "to_images", "to_text", "to_markdown", "linearize"
      - level: "high", "medium", "lossless" (for compress)
      - image_format: "png" or "jpeg" (for to_images)
      - dpi: int (default 150)
    """
    if not data:
        raise EngineError("Input document data is empty.")

    options = options or {}
    mode = str(options.get("mode", "compress")).lower()

    if mode == "compress":
        level = options.get("level", "medium").lower()
        doc = _open_pdf(data)
        try:
            if level == "high":
                # Downsample images within the document
                for page in doc:
                    for img_info in page.get_images():
                        xref = img_info[0]
                        try:
                            base_img = doc.extract_image(xref)
                            if base_img and "image" in base_img:
                                pil_img = Image.open(BytesIO(base_img["image"]))
                                if pil_img.width > 1200 or pil_img.height > 1200:
                                    pil_img.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
                                    buf = BytesIO()
                                    pil_img.convert("RGB").save(buf, format="JPEG", quality=75, optimize=True)
                                    doc.update_stream(xref, buf.getvalue())
                        except Exception:
                            pass
                out_bytes = doc.tobytes(garbage=4, deflate=True, clean=True)
            elif level == "lossless":
                out_bytes = doc.tobytes(garbage=3, deflate=True)
            else:  # balanced medium
                out_bytes = doc.tobytes(garbage=4, deflate=True, clean=True)
        finally:
            doc.close()
        return out_bytes, "application/pdf", "compressed.pdf"

    elif mode == "grayscale":
        doc = _open_pdf(data)
        out_doc = fitz.open()
        try:
            dpi = int(options.get("dpi", 150))
            for page in doc:
                pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
                img_data = pix.tobytes("png")
                img_doc = fitz.open(stream=img_data, filetype="png")
                rect = page.rect
                new_page = out_doc.new_page(width=rect.width, height=rect.height)
                new_page.insert_image(rect, stream=img_data)
                img_doc.close()
            out_bytes = out_doc.tobytes(garbage=3, deflate=True)
        finally:
            doc.close()
            out_doc.close()
        return out_bytes, "application/pdf", "grayscale.pdf"

    elif mode in ("to_images", "pdf_to_images"):
        doc = _open_pdf(data)
        fmt = options.get("image_format", "png").lower()
        dpi = int(options.get("dpi", 150))
        try:
            if doc.page_count == 1:
                pix = doc[0].get_pixmap(dpi=dpi)
                img_bytes = pix.tobytes(fmt)
                doc.close()
                mime = f"image/{fmt}"
                return img_bytes, mime, f"page_1.{fmt}"
            else:
                zip_buffer = BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    for i, page in enumerate(doc):
                        pix = page.get_pixmap(dpi=dpi)
                        zf.writestr(f"page_{i+1}.{fmt}", pix.tobytes(fmt))
                doc.close()
                return zip_buffer.getvalue(), "application/zip", "document_images.zip"
        except Exception:
            doc.close()
            raise

    elif mode in ("to_text", "pdf_to_text"):
        doc = _open_pdf(data)
        parts = []
        try:
            for i, page in enumerate(doc):
                text = page.get_text("text").strip()
                parts.append(f"--- Page {i+1} ---\n{text}")
            full_text = "\n\n".join(parts)
        finally:
            doc.close()
        return full_text.encode("utf-8"), "text/plain; charset=utf-8", "extracted_text.txt"

    elif mode in ("to_markdown", "pdf_to_markdown"):
        doc = _open_pdf(data)
        md_lines = ["# Document Text Export\n"]
        try:
            for i, page in enumerate(doc):
                md_lines.append(f"## Page {i+1}\n")
                blocks = page.get_text("blocks")
                for b in blocks:
                    text = b[4].strip()
                    if text:
                        md_lines.append(f"{text}\n")
            full_md = "\n".join(md_lines)
        finally:
            doc.close()
        return full_md.encode("utf-8"), "text/markdown; charset=utf-8", "document.md"

    elif mode == "linearize":
        # Fast web view linearization via pikepdf
        try:
            with pikepdf.open(BytesIO(data)) as pdf:
                buf = BytesIO()
                pdf.save(buf, linearize=True)
                return buf.getvalue(), "application/pdf", "linearized.pdf"
        except Exception as exc:
            raise EngineError(f"Linearization error: {exc}") from exc

    else:
        raise EngineError(f"Unsupported transformation mode '{mode}'")


# ---------------------------------------------------------------------------
# 3. PROTECT PRIMITIVE
# ---------------------------------------------------------------------------

def engine_protect(
    data: bytes,
    options: dict[str, Any] | None = None,
    cert_data: bytes | None = None,
) -> tuple[bytes, str, str]:
    """
    Security, integrity, encryption, and redaction operations.
    Options:
      - mode: "encrypt", "decrypt", "sanitize", "redact", "sign"
      - password: str (for encrypt/decrypt/sign)
      - owner_password: str (for encrypt)
      - allow_printing: bool (default True)
      - allow_copying: bool (default True)
      - redact_terms: list of strings to redact
      - cert_password: str (for sign)
    """
    if not data:
        raise EngineError("Input document data is empty.")

    options = options or {}
    mode = str(options.get("mode", "encrypt")).lower()

    if mode == "encrypt":
        user_pw = str(options.get("password") or "")
        owner_pw = str(options.get("owner_password") or user_pw)
        if not user_pw and not owner_pw:
            raise EngineError("Encryption requires a password.")
        
        allow_print = options.get("allow_printing", True)
        allow_copy = options.get("allow_copying", True)
        
        perm = fitz.PDF_PERM_ACCESSIBILITY
        if allow_print:
            perm |= fitz.PDF_PERM_PRINT
        if allow_copy:
            perm |= fitz.PDF_PERM_COPY

        doc = _open_pdf(data)
        try:
            out_bytes = doc.tobytes(
                encryption=fitz.PDF_ENCRYPT_AES_256,
                user_pw=user_pw,
                owner_pw=owner_pw,
                permissions=perm,
            )
        finally:
            doc.close()
        return out_bytes, "application/pdf", "encrypted.pdf"

    elif mode == "decrypt":
        password = str(options.get("password") or "")
        doc = _open_pdf(data, password=password)
        try:
            out_bytes = doc.tobytes(encryption=fitz.PDF_ENCRYPT_NONE)
        finally:
            doc.close()
        return out_bytes, "application/pdf", "decrypted.pdf"

    elif mode == "sanitize":
        # Scrub metadata, attachments, private tags, annotations
        doc = _open_pdf(data)
        try:
            doc.set_metadata({})
            for page in doc:
                # Remove all links and annotations
                ann = page.first_annot
                while ann:
                    nxt = ann.next
                    page.delete_annot(ann)
                    ann = nxt
            out_bytes = doc.tobytes(garbage=4, deflate=True, clean=True)
        finally:
            doc.close()
        return out_bytes, "application/pdf", "sanitized.pdf"

    elif mode == "redact":
        terms = options.get("redact_terms", [])
        if isinstance(terms, str):
            terms = [t.strip() for t in terms.split(",") if t.strip()]
        if not terms:
            raise EngineError("Redaction mode requires 'redact_terms'.")
        
        doc = _open_pdf(data)
        try:
            for page in doc:
                for term in terms:
                    rects = page.search_for(term)
                    for r in rects:
                        page.add_redact_annot(r, fill=(0, 0, 0))
                page.apply_redactions()
            out_bytes = doc.tobytes(garbage=3, deflate=True)
        finally:
            doc.close()
        return out_bytes, "application/pdf", "redacted.pdf"

    elif mode == "sign":
        if not cert_data:
            raise EngineError("Digital signing requires a PKCS#12 (.p12/.pfx) certificate file.")
        cert_pw = str(options.get("cert_password") or options.get("password") or "")
        try:
            signer = signers.load_crypto(
                key_file=BytesIO(cert_data),
                passphrase=cert_pw.encode("utf-8") if cert_pw else None,
            )
            w = IncrementalPdfFileWriter(BytesIO(data))
            out_buf = BytesIO()
            signers.sign_pdf(
                w,
                signers.PdfSignatureMetadata(field_name="Signature1"),
                signer=signer,
                output=out_buf,
            )
            return out_buf.getvalue(), "application/pdf", "signed.pdf"
        except Exception as exc:
            raise EngineError(f"Digital signature failed: {exc}") from exc

    else:
        raise EngineError(f"Unsupported protect mode '{mode}'")


# ---------------------------------------------------------------------------
# 4. INSPECT PRIMITIVE
# ---------------------------------------------------------------------------

def engine_inspect(
    data: bytes,
    second_data: bytes | None = None,
) -> dict[str, Any]:
    """
    Structural telemetry, document intelligence, font inventory, and diff.
    """
    doc = _open_pdf(data)
    try:
        page_summaries = []
        font_set = set()
        img_count = 0
        for i, page in enumerate(doc):
            r = page.rect
            for font in page.get_fonts():
                font_set.add(font[3] if len(font) > 3 else font[0])
            images = page.get_images()
            img_count += len(images)
            page_summaries.append({
                "page": i + 1,
                "width": round(r.width, 2),
                "height": round(r.height, 2),
                "rotation": page.rotation,
                "images": len(images),
                "text_length": len(page.get_text("text").strip()),
            })

        meta = doc.metadata or {}
        report: dict[str, Any] = {
            "page_count": doc.page_count,
            "is_encrypted": doc.is_encrypted,
            "metadata": {
                "title": meta.get("title", ""),
                "author": meta.get("author", ""),
                "subject": meta.get("subject", ""),
                "creator": meta.get("creator", ""),
                "producer": meta.get("producer", ""),
            },
            "fonts": sorted(list(font_set)),
            "total_images": img_count,
            "pages": page_summaries,
        }

        # If a second document is supplied, compute comparative telemetry
        if second_data:
            doc2 = _open_pdf(second_data)
            try:
                text1 = "\n".join(p.get_text("text") for p in doc)
                text2 = "\n".join(p.get_text("text") for p in doc2)
                diff = list(difflib.unified_diff(
                    text1.splitlines(),
                    text2.splitlines(),
                    fromfile="doc1.pdf",
                    tofile="doc2.pdf",
                    lineterm="",
                ))
                report["comparison"] = {
                    "doc1_pages": doc.page_count,
                    "doc2_pages": doc2.page_count,
                    "text_identical": text1 == text2,
                    "diff_lines_count": len(diff),
                    "diff_sample": diff[:40],
                }
            finally:
                doc2.close()

        return report
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 5. ATOMIC COMPOSABLE PIPELINE
# ---------------------------------------------------------------------------

def engine_pipeline(
    files: Sequence[tuple[str, bytes]],
    recipe: list[dict[str, Any]],
    cert_data: bytes | None = None,
) -> tuple[bytes, str, str]:
    """
    Executes an arbitrary chain of verbs sequentially in memory.
    Example recipe:
      [
        {"verb": "compose", "options": {"page_selection": "1-5", "rotation": 90}},
        {"verb": "transform", "options": {"mode": "compress", "level": "medium"}},
        {"verb": "protect", "options": {"mode": "encrypt", "password": "secret"}}
      ]
    """
    if not files:
        raise EngineError("Pipeline requires at least one document.")
    if not recipe:
        # Default: compose inputs
        return engine_compose(files)

    current_data: bytes | None = None
    current_name = "output.pdf"
    current_mime = "application/pdf"

    for step in recipe:
        verb = step.get("verb", "").lower()
        options = step.get("options", {})

        if verb == "compose":
            if current_data is None:
                step_files = files
            else:
                step_files = [(current_name, current_data)]
            current_data, current_mime, current_name = engine_compose(step_files, options)

        elif verb == "transform":
            if current_data is None:
                current_data = files[0][1]
            current_data, current_mime, current_name = engine_transform(current_data, options)

        elif verb == "protect":
            if current_data is None:
                current_data = files[0][1]
            current_data, current_mime, current_name = engine_protect(current_data, options, cert_data=cert_data)

        else:
            raise EngineError(f"Unknown pipeline verb: '{verb}'")

    if current_data is None:
        raise EngineError("Pipeline produced no output.")
    return current_data, current_mime, current_name
