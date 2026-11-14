import io
import json
import unittest

import pymupdf as fitz
from PIL import Image
from fastapi.testclient import TestClient

from app.engine import (
    EngineError,
    engine_compose,
    engine_inspect,
    engine_pipeline,
    engine_protect,
    engine_transform,
    parse_page_range,
)
from app.main import app


def _create_test_pdf(num_pages: int = 3, text_prefix: str = "Page") -> bytes:
    doc = fitz.open()
    for i in range(num_pages):
        page = doc.new_page(width=595.28, height=841.89)
        page.insert_text((50, 100), f"{text_prefix} {i+1} content")
    out = doc.tobytes()
    doc.close()
    return out


def _create_test_image(width: int = 100, height: int = 100, color: str = "red") -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (width, height), color=color)
    img.save(buf, format="PNG")
    return buf.getvalue()


class PrismEngineTests(unittest.TestCase):
    def setUp(self):
        self.pdf1 = _create_test_pdf(3, "Doc1 Page")
        self.pdf2 = _create_test_pdf(2, "Doc2 Page")
        self.img1 = _create_test_image(200, 200, "blue")
        self.client = TestClient(app)

    def test_parse_page_range(self):
        self.assertEqual(parse_page_range("1-3, 5", 5), [0, 1, 2, 4])
        self.assertEqual(parse_page_range("all", 3), [0, 1, 2])
        with self.assertRaises(EngineError):
            parse_page_range("1-10", 3)

    def test_compose_merge_and_reorder(self):
        plan = {
            "page_selection": "1-2, 5",
            "rotation": 90,
            "insert_blanks": [1],
        }
        out_bytes, mime, name = engine_compose(
            [("doc1.pdf", self.pdf1), ("doc2.pdf", self.pdf2)],
            plan,
        )
        self.assertEqual(mime, "application/pdf")
        doc = fitz.open(stream=out_bytes, filetype="pdf")
        self.assertEqual(doc.page_count, 4)
        self.assertEqual(doc[0].rotation, 90)
        doc.close()

    def test_compose_with_image(self):
        out_bytes, mime, name = engine_compose(
            [("doc1.pdf", self.pdf1), ("image.png", self.img1)],
            {},
        )
        self.assertEqual(mime, "application/pdf")
        doc = fitz.open(stream=out_bytes, filetype="pdf")
        self.assertEqual(doc.page_count, 4)
        # Verify the image page was created as standard A4 full page
        img_page = doc[3]
        self.assertAlmostEqual(img_page.rect.width, 595.28, delta=1.0)
        self.assertAlmostEqual(img_page.rect.height, 841.89, delta=1.0)
        doc.close()

    def test_compose_reverse_and_nup(self):
        out_bytes, mime, name = engine_compose(
            [("doc1.pdf", self.pdf1)],
            {"reverse": True, "nup": 2},
        )
        self.assertEqual(mime, "application/pdf")
        doc = fitz.open(stream=out_bytes, filetype="pdf")
        self.assertEqual(doc.page_count, 2)  # 3 pages laid out 2-up -> 2 sheets
        doc.close()

    def test_transform_compress(self):
        for lvl in ["high", "medium", "lossless"]:
            comp_bytes, mime, name = engine_transform(self.pdf1, {"mode": "compress", "level": lvl})
            self.assertEqual(mime, "application/pdf")
            self.assertTrue(len(comp_bytes) > 0)

    def test_transform_grayscale(self):
        gray_bytes, mime, name = engine_transform(self.pdf1, {"mode": "grayscale", "dpi": 72})
        self.assertEqual(mime, "application/pdf")
        self.assertTrue(len(gray_bytes) > 0)

    def test_transform_extract_text_and_markdown(self):
        # Text
        text_bytes, mime, name = engine_transform(self.pdf1, {"mode": "to_text"})
        self.assertEqual(mime, "text/plain; charset=utf-8")
        self.assertIn(b"Doc1 Page 1 content", text_bytes)

        # Markdown
        md_bytes, mime, name = engine_transform(self.pdf1, {"mode": "to_markdown"})
        self.assertEqual(mime, "text/markdown; charset=utf-8")
        self.assertIn(b"# Document Text Export", md_bytes)

    def test_transform_to_images(self):
        # Multi-page returns zip
        img_bytes, mime, name = engine_transform(self.pdf1, {"mode": "to_images", "image_format": "png"})
        self.assertEqual(mime, "application/zip")
        self.assertTrue(len(img_bytes) > 0)

        # Single page returns image directly
        single_pdf = _create_test_pdf(1, "Single")
        single_bytes, mime, name = engine_transform(single_pdf, {"mode": "to_images", "image_format": "png"})
        self.assertEqual(mime, "image/png")
        self.assertTrue(len(single_bytes) > 0)

    def test_transform_linearize(self):
        lin_bytes, mime, name = engine_transform(self.pdf1, {"mode": "linearize"})
        self.assertEqual(mime, "application/pdf")
        self.assertTrue(len(lin_bytes) > 0)

    def test_protect_encrypt_and_decrypt(self):
        enc_bytes, mime, name = engine_protect(self.pdf1, {"mode": "encrypt", "password": "password123"})
        self.assertEqual(mime, "application/pdf")

        # Verify password is required
        doc = fitz.open(stream=enc_bytes, filetype="pdf")
        self.assertTrue(doc.is_encrypted)
        self.assertTrue(doc.authenticate("password123"))
        doc.close()

        # Decrypt
        dec_bytes, mime, name = engine_protect(enc_bytes, {"mode": "decrypt", "password": "password123"})
        doc2 = fitz.open(stream=dec_bytes, filetype="pdf")
        self.assertFalse(doc2.is_encrypted)
        doc2.close()

    def test_protect_sanitize(self):
        san_bytes, mime, name = engine_protect(self.pdf1, {"mode": "sanitize"})
        self.assertEqual(mime, "application/pdf")
        doc = fitz.open(stream=san_bytes, filetype="pdf")
        self.assertEqual(doc.metadata.get("title", ""), "")
        doc.close()

    def test_protect_redact(self):
        red_bytes, mime, name = engine_protect(self.pdf1, {"mode": "redact", "redact_terms": ["Doc1 Page 1"]})
        self.assertEqual(mime, "application/pdf")
        doc = fitz.open(stream=red_bytes, filetype="pdf")
        text = doc[0].get_text()
        self.assertNotIn("Doc1 Page 1", text)
        doc.close()

    def test_inspect_and_diff(self):
        report = engine_inspect(self.pdf1, self.pdf2)
        self.assertEqual(report["page_count"], 3)
        self.assertIn("pages", report)
        self.assertIn("comparison", report)
        self.assertEqual(report["comparison"]["doc1_pages"], 3)
        self.assertEqual(report["comparison"]["doc2_pages"], 2)

    def test_pipeline_atomic_execution(self):
        recipe = [
            {"verb": "compose", "options": {"page_selection": "1-2", "rotation": 180}},
            {"verb": "transform", "options": {"mode": "compress", "level": "medium"}},
            {"verb": "protect", "options": {"mode": "encrypt", "password": "safe"}},
        ]
        out_bytes, mime, name = engine_pipeline(
            [("source.pdf", self.pdf1)],
            recipe,
        )
        self.assertEqual(mime, "application/pdf")
        doc = fitz.open(stream=out_bytes, filetype="pdf")
        self.assertTrue(doc.is_encrypted)
        self.assertTrue(doc.authenticate("safe"))
        self.assertEqual(doc.page_count, 2)
        self.assertEqual(doc[0].rotation, 180)
        doc.close()

    def test_fastapi_endpoints(self):
        headers = {"Origin": "http://127.0.0.1:8000"}

        # Health & Discovery
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/api").status_code, 200)
        self.assertEqual(self.client.get("/api/tools").status_code, 200)
        self.assertEqual(self.client.get("/api/status").status_code, 200)

        # /api/v1/compose
        resp = self.client.post(
            "/api/v1/compose",
            files=[("files", ("test.pdf", self.pdf1, "application/pdf"))],
            data={"plan": json.dumps({"page_selection": "1-2"})},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/pdf")

        # /api/v1/transform
        resp = self.client.post(
            "/api/v1/transform",
            files=[("files", ("test.pdf", self.pdf1, "application/pdf"))],
            data={"options": json.dumps({"mode": "to_text"})},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Doc1 Page 1", resp.text)

        # /api/v1/protect
        resp = self.client.post(
            "/api/v1/protect",
            files=[("files", ("test.pdf", self.pdf1, "application/pdf"))],
            data={"options": json.dumps({"mode": "encrypt", "password": "abc"})},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200)

        # /api/v1/inspect
        resp = self.client.post(
            "/api/v1/inspect",
            files=[("files", ("test.pdf", self.pdf1, "application/pdf"))],
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["page_count"], 3)

        # /api/v1/pipeline
        recipe = [
            {"verb": "compose", "options": {"page_selection": "1"}},
            {"verb": "transform", "options": {"mode": "compress"}},
        ]
        resp = self.client.post(
            "/api/v1/pipeline",
            files=[("files", ("test.pdf", self.pdf1, "application/pdf"))],
            data={"recipe": json.dumps(recipe)},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
