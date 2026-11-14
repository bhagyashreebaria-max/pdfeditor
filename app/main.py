"""
Prism Document Studio & Engine - HTTP Service
----------------------------------------------
Provides the HTTP API and serves the interactive Document Studio.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.engine import (
    EngineError,
    MAX_FILE_BYTES,
    engine_compose,
    engine_inspect,
    engine_pipeline,
    engine_protect,
    engine_transform,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prism-document-engine")

STATIC_DIR = Path(__file__).parent / "static"
app = FastAPI(title="Prism Document Studio & Engine", docs_url=None, redoc_url=None)

# Concurrency & Resource Bounding
try:
    MAX_CONCURRENT_JOBS = int(os.environ.get("PRISM_MAX_CONCURRENT_JOBS", "8"))
except ValueError:
    MAX_CONCURRENT_JOBS = 8

MAX_INPUT_FILES = 64
MAX_UPLOAD_BYTES = MAX_FILE_BYTES  # 20 GiB

processing_slots = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

configured_origins = os.environ.get("PRISM_ALLOWED_ORIGINS") or os.environ.get("PAPERTRAIL_ALLOWED_ORIGINS")
ALLOWED_ORIGINS = {
    origin.strip()
    for origin in (
        configured_origins.split(",")
        if configured_origins
        else ["http://127.0.0.1:8000", "http://localhost:8000"]
    )
    if origin.strip()
}

allowed_hosts = [
    host.strip()
    for host in (os.environ.get("PRISM_ALLOWED_HOSTS") or os.environ.get("PAPERTRAIL_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver")).split(",")
    if host.strip()
]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)


@app.middleware("http")
async def enforce_origin_and_headers(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin and ALLOWED_ORIGINS and origin not in ALLOWED_ORIGINS:
        return JSONResponse(
            status_code=403,
            content={"detail": "Request origin is not allowed by engine policy."},
        )
    response = await call_next(request)
    if origin and origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Request-ID"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.get("/health")
async def health_check():
    return {"status": "ok", "engine": "prism-document-engine", "concurrency_limit": MAX_CONCURRENT_JOBS}


@app.get("/api")
async def api_index():
    return {
        "engine": "prism-document-engine",
        "architecture": "composable-primitives",
        "version": "2.0.0",
        "primitives": [
            {"verb": "compose", "path": "/api/v1/compose", "description": "Multi-document merge, slice, reorder, rotate, blank, N-up"},
            {"verb": "transform", "path": "/api/v1/transform", "description": "Compression, grayscale, image/text extraction, linearization"},
            {"verb": "protect", "path": "/api/v1/protect", "description": "AES-256 encryption, password removal, redaction, digital signing"},
            {"verb": "inspect", "path": "/api/v1/inspect", "description": "Document structure telemetry, fonts, images, and diff report"},
            {"verb": "pipeline", "path": "/api/v1/pipeline", "description": "Atomic chained multi-op execution in memory"},
        ],
        "endpoints": [
            "/api",
            "/api/tools",
            "/api/status",
            "/api/inspect",
            "/api/process/{tool}",
            "/api/v1/compose",
            "/api/v1/transform",
            "/api/v1/protect",
            "/api/v1/inspect",
            "/api/v1/pipeline",
        ],
    }


@app.get("/api/tools")
@app.get("/api/engine/capabilities")
async def get_capabilities():
    """Return engine capabilities."""
    return [
        {"id": "compose", "name": "Document Composer", "category": "Core Primitives", "status": "ready"},
        {"id": "transform", "name": "Document Transformer", "category": "Core Primitives", "status": "ready"},
        {"id": "protect", "name": "Security & Protection", "category": "Core Primitives", "status": "ready"},
        {"id": "inspect", "name": "Structural Telemetry & Diff", "category": "Core Primitives", "status": "ready"},
        {"id": "pipeline", "name": "Composable Pipeline", "category": "Core Primitives", "status": "ready"},
        # Aliases for compatibility
        {"id": "merge", "name": "Merge", "category": "Compose", "status": "ready"},
        {"id": "split", "name": "Split & Slice", "category": "Compose", "status": "ready"},
        {"id": "rotate", "name": "Rotate", "category": "Compose", "status": "ready"},
        {"id": "reorder", "name": "Reorder", "category": "Compose", "status": "ready"},
        {"id": "compress", "name": "Compress", "category": "Transform", "status": "ready"},
        {"id": "encrypt", "name": "Encrypt", "category": "Protect", "status": "ready"},
        {"id": "decrypt", "name": "Decrypt", "category": "Protect", "status": "ready"},
    ]


@app.get("/api/status")
async def get_status():
    return {
        "status": "ready",
        "engine": "prism-document-engine",
        "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
        "max_upload_bytes": MAX_FILE_BYTES,
    }


# ---------------------------------------------------------------------------
# CORE V1 ENDPOINTS
# ---------------------------------------------------------------------------

@app.post("/api/v1/compose")
async def api_compose(
    files: List[UploadFile] = File(...),
    plan: str = Form("{}"),
):
    try:
        plan_dict = json.loads(plan) if plan else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON in 'plan': {exc}")

    file_payloads = []
    for f in files:
        data = await f.read()
        file_payloads.append((f.filename or "document.pdf", data))

    async with processing_slots:
        try:
            out_bytes, mime, name = await run_in_threadpool(engine_compose, file_payloads, plan_dict)
        except EngineError as err:
            raise HTTPException(status_code=err.status_code, detail=err.message)

    return Response(
        content=out_bytes,
        media_type=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Output-Bytes": str(len(out_bytes)),
        },
    )


@app.post("/api/v1/transform")
async def api_transform(
    files: List[UploadFile] = File(...),
    options: str = Form("{}"),
):
    if not files:
        raise HTTPException(status_code=400, detail="Document file is required.")

    try:
        opt_dict = json.loads(options) if options else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON in 'options': {exc}")

    data = await files[0].read()

    async with processing_slots:
        try:
            out_bytes, mime, name = await run_in_threadpool(engine_transform, data, opt_dict)
        except EngineError as err:
            raise HTTPException(status_code=err.status_code, detail=err.message)

    return Response(
        content=out_bytes,
        media_type=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Output-Bytes": str(len(out_bytes)),
        },
    )


@app.post("/api/v1/protect")
async def api_protect(
    files: List[UploadFile] = File(...),
    certificate: UploadFile = File(None),
    options: str = Form("{}"),
):
    if not files:
        raise HTTPException(status_code=400, detail="Document file is required.")

    try:
        opt_dict = json.loads(options) if options else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON in 'options': {exc}")

    data = await files[0].read()
    cert_data = await certificate.read() if certificate else None

    async with processing_slots:
        try:
            out_bytes, mime, name = await run_in_threadpool(engine_protect, data, opt_dict, cert_data)
        except EngineError as err:
            raise HTTPException(status_code=err.status_code, detail=err.message)

    return Response(
        content=out_bytes,
        media_type=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Output-Bytes": str(len(out_bytes)),
        },
    )


@app.post("/api/v1/inspect")
@app.post("/api/inspect")
async def api_inspect(
    files: List[UploadFile] = File(...),
):
    if not files:
        raise HTTPException(status_code=400, detail="Document file is required.")

    data1 = await files[0].read()
    data2 = await files[1].read() if len(files) > 1 else None

    async with processing_slots:
        try:
            report = await run_in_threadpool(engine_inspect, data1, data2)
        except EngineError as err:
            raise HTTPException(status_code=err.status_code, detail=err.message)

    return report


@app.post("/api/v1/pipeline")
async def api_pipeline(
    files: List[UploadFile] = File(...),
    recipe: str = Form("[]"),
    certificate: UploadFile = File(None),
):
    try:
        recipe_list = json.loads(recipe) if recipe else []
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON in 'recipe': {exc}")

    file_payloads = []
    for f in files:
        data = await f.read()
        file_payloads.append((f.filename or "document.pdf", data))

    cert_data = await certificate.read() if certificate else None

    async with processing_slots:
        try:
            out_bytes, mime, name = await run_in_threadpool(engine_pipeline, file_payloads, recipe_list, cert_data)
        except EngineError as err:
            raise HTTPException(status_code=err.status_code, detail=err.message)

    return Response(
        content=out_bytes,
        media_type=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Output-Bytes": str(len(out_bytes)),
        },
    )


# ---------------------------------------------------------------------------
# ADAPTER LAYER FOR COMPATIBILITY WITH EXISTING AUTOMATION & TESTS
# ---------------------------------------------------------------------------

@app.post("/api/process/{tool_id}")
async def process_tool_adapter(
    tool_id: str,
    request: Request,
    files: List[UploadFile] = File(default=[]),
    certificate: UploadFile = File(default=None),
):
    # Form data extraction
    form_data = await request.form()
    
    # Check total upload size
    total_in = 0
    file_list = []
    for f in files:
        content = await f.read()
        total_in += len(content)
        file_list.append((f.filename or "input.pdf", content))

    if total_in > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="Payload exceeds maximum permitted size.")

    cert_content = await certificate.read() if certificate else None

    # Handle actions via engine primitives
    tid = tool_id.lower()
    async with processing_slots:
        try:
            if tid == "merge":
                ranges = form_data.get("ranges")
                plan = {"page_selection": ranges} if ranges else {}
                out_bytes, mime, name = await run_in_threadpool(engine_compose, file_list, plan)

            elif tid in ("split", "extract"):
                ranges = form_data.get("ranges") or form_data.get("page_ranges") or "1"
                plan = {"page_selection": str(ranges)}
                out_bytes, mime, name = await run_in_threadpool(engine_compose, file_list, plan)

            elif tid == "rotate":
                angle = int(form_data.get("angle", 90))
                plan = {"rotation": angle}
                out_bytes, mime, name = await run_in_threadpool(engine_compose, file_list, plan)

            elif tid == "reorder":
                order = form_data.get("order") or form_data.get("page_order")
                plan = {"page_selection": str(order)} if order else {}
                out_bytes, mime, name = await run_in_threadpool(engine_compose, file_list, plan)

            elif tid == "reverse-pages":
                plan = {"reverse": True}
                out_bytes, mime, name = await run_in_threadpool(engine_compose, file_list, plan)

            elif tid == "insert-blank":
                after = form_data.get("after_page") or "1"
                plan = {"insert_blanks": [int(after)]}
                out_bytes, mime, name = await run_in_threadpool(engine_compose, file_list, plan)

            elif tid == "image-to-pdf":
                out_bytes, mime, name = await run_in_threadpool(engine_compose, file_list, {})

            elif tid == "compress":
                level = form_data.get("level", "medium")
                out_bytes, mime, name = await run_in_threadpool(engine_transform, file_list[0][1], {"mode": "compress", "level": level})

            elif tid == "grayscale":
                out_bytes, mime, name = await run_in_threadpool(engine_transform, file_list[0][1], {"mode": "grayscale"})

            elif tid == "linearize":
                out_bytes, mime, name = await run_in_threadpool(engine_transform, file_list[0][1], {"mode": "linearize"})

            elif tid == "pdf-to-images":
                fmt = form_data.get("format", "png")
                out_bytes, mime, name = await run_in_threadpool(engine_transform, file_list[0][1], {"mode": "to_images", "image_format": fmt})

            elif tid == "pdf-to-text":
                out_bytes, mime, name = await run_in_threadpool(engine_transform, file_list[0][1], {"mode": "to_text"})

            elif tid == "pdf-to-markdown":
                out_bytes, mime, name = await run_in_threadpool(engine_transform, file_list[0][1], {"mode": "to_markdown"})

            elif tid == "encrypt":
                pw = form_data.get("password") or form_data.get("user_password") or ""
                owner_pw = form_data.get("owner_password") or pw
                out_bytes, mime, name = await run_in_threadpool(
                    engine_protect, file_list[0][1], {"mode": "encrypt", "password": pw, "owner_password": owner_pw}
                )

            elif tid == "decrypt":
                pw = form_data.get("password") or ""
                out_bytes, mime, name = await run_in_threadpool(
                    engine_protect, file_list[0][1], {"mode": "decrypt", "password": pw}
                )

            elif tid in ("sanitize", "remove-metadata"):
                out_bytes, mime, name = await run_in_threadpool(
                    engine_protect, file_list[0][1], {"mode": "sanitize"}
                )

            elif tid == "sign":
                pw = form_data.get("password") or form_data.get("cert_password") or ""
                out_bytes, mime, name = await run_in_threadpool(
                    engine_protect, file_list[0][1], {"mode": "sign", "password": pw}, cert_data=cert_content
                )

            elif tid == "compare-text":
                doc2 = file_list[1][1] if len(file_list) > 1 else None
                report = await run_in_threadpool(engine_inspect, file_list[0][1], doc2)
                return JSONResponse(content=report)

            elif tid == "batch":
                steps_json = form_data.get("steps") or "[]"
                recipe = json.loads(steps_json)
                out_bytes, mime, name = await run_in_threadpool(
                    engine_pipeline, file_list, recipe, cert_data=cert_content
                )

            else:
                raise HTTPException(status_code=404, detail=f"Operation '{tool_id}' is not supported or has been retired.")

        except EngineError as err:
            raise HTTPException(status_code=err.status_code, detail=err.message)

    return Response(
        content=out_bytes,
        media_type=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Input-Bytes": str(total_in),
            "X-Output-Bytes": str(len(out_bytes)),
        },
    )


# Serve Static Assets
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
