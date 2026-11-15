"""Local FastAPI adapter for a ChargeCheck lookup function.

Set CHARGECHECK_BACKEND to "your_module:your_function". The function should
accept one query string and return JSON-compatible data, ideally a list of
records with code, description, billed_price, and reference_price fields.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chargecheck")
MAX_PDF_BYTES = 15 * 1024 * 1024

app = FastAPI(title="ChargeCheck local lookup API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["null"],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def load_backend() -> Any:
    """Load the configured module:function adapter, reporting setup errors clearly."""
    spec = os.environ.get("CHARGECHECK_BACKEND", "")
    module_name, separator, function_name = spec.partition(":")
    if not separator or not module_name or not function_name:
        raise HTTPException(
            status_code=503,
            detail=(
                "Backend is not configured. Set CHARGECHECK_BACKEND to "
                "'module_name:function_name'."
            ),
        )

    try:
        module = importlib.import_module(module_name)
        backend = getattr(module, function_name)
    except (ImportError, AttributeError) as error:
        logger.exception("Could not load configured backend %r", spec)
        raise HTTPException(
            status_code=503,
            detail=f"Could not load configured backend {spec!r}. Check the module and function name.",
        ) from error

    if not callable(backend):
        raise HTTPException(
            status_code=503,
            detail=f"Configured backend {spec!r} is not callable.",
        )
    return backend


@app.get("/lookup")
async def lookup(query: str = Query(min_length=1, max_length=200)) -> Any:
    """Forward a search query to the user's existing Python lookup function."""
    backend = load_backend()
    try:
        if inspect.iscoroutinefunction(backend):
            result = await backend(query)
        else:
            result = await run_in_threadpool(backend, query)
            if inspect.isawaitable(result):
                result = await result
        return result
    except HTTPException:
        raise
    except Exception as error:
        logger.exception("ChargeCheck backend lookup failed for query %r", query)
        raise HTTPException(
            status_code=502,
            detail="The configured billing lookup function failed.",
        ) from error


@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)) -> dict[str, Any]:
    """Validate a PDF and forward its filename and bytes to a configured processor."""
    filename = file.filename or "bill.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=415, detail="Choose a PDF file.")

    content = await file.read(MAX_PDF_BYTES + 1)
    if len(content) > MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF files must be 15 MB or smaller.")
    if not content.startswith(b"%PDF-"):
        raise HTTPException(status_code=415, detail="The uploaded file is not a valid PDF.")

    spec = os.environ.get("CHARGECHECK_PDF_BACKEND", "")
    module_name, separator, function_name = spec.partition(":")
    if not separator or not module_name or not function_name:
        raise HTTPException(
            status_code=503,
            detail=(
                "PDF processing is not configured. Set CHARGECHECK_PDF_BACKEND to "
                "'module_name:function_name'. The function must accept (filename, pdf_bytes)."
            ),
        )

    try:
        module = importlib.import_module(module_name)
        backend = getattr(module, function_name)
        if not callable(backend):
            raise TypeError(f"Configured PDF backend {spec!r} is not callable.")
        if inspect.iscoroutinefunction(backend):
            results = await backend(filename, content)
        else:
            results = await run_in_threadpool(backend, filename, content)
            if inspect.isawaitable(results):
                results = await results
    except HTTPException:
        raise
    except Exception as error:
        logger.exception("Could not process uploaded PDF %r using %r", filename, spec)
        raise HTTPException(
            status_code=502,
            detail="The configured PDF processor failed to analyze the uploaded file.",
        ) from error
    finally:
        await file.close()

    return {"filename": filename, "results": results}
