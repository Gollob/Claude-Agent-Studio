"""
file-intake: Telegram bot + FastAPI web upload.
Entry point — runs both concurrently in one process.
"""
import asyncio
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes,
)
from fastapi import FastAPI, UploadFile, File, Header, HTTPException
from fastapi.responses import HTMLResponse
import uvicorn

import trilium
from handlers import image as img_handler
from handlers import pdf as pdf_handler
from handlers import audio as audio_handler

# ── OpenTelemetry init (ADR-006/ADR-007: direct to Uptrace, graceful degradation) ──
def _init_otel():  # type: ignore[return]
    """Initialise OTel SDK.  Returns FastAPIInstrumentor class or None.

    Errors are logged and swallowed so the service starts even if Uptrace is
    unreachable or misconfigured.  The caller must invoke
    ``FastAPIInstrumentor.instrument_app(web)`` after the FastAPI instance is
    created — ``instrument()`` without an app patches __init__ but does NOT add
    middleware to already-created instances.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        if not endpoint:
            logging.getLogger("file-intake.otel").warning(
                "OTEL_EXPORTER_OTLP_ENDPOINT not set — tracing disabled"
            )
            return None

        service_name = os.getenv("OTEL_SERVICE_NAME", "file-intake")
        resource = Resource.create({
            "service.name": service_name,
            "service.namespace": os.getenv("OTEL_SERVICE_NAMESPACE", "agent-studio"),
            "deployment.environment": os.getenv("OTEL_DEPLOYMENT_ENV", "production"),
        })

        # DSN token for Uptrace ingest authentication.
        # uptrace-dsn header format: http://<project_token>@uptrace:14318/<project_id>
        # project_id=1 is the first (and only) project seeded in uptrace.yml.
        headers: dict[str, str] = {}
        project_token = os.getenv("UPTRACE_PROJECT_TOKEN", "")
        if project_token:
            headers["uptrace-dsn"] = f"http://{project_token}@uptrace:14318/1"

        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            headers=headers,
            timeout=5,  # don't block startup on unreachable backend
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(
                exporter,
                max_queue_size=512,
                max_export_batch_size=64,
                export_timeout_millis=5000,
            )
        )
        trace.set_tracer_provider(provider)

        HTTPXClientInstrumentor().instrument()
        LoggingInstrumentor().instrument(set_logging_format=False)

        logging.getLogger("file-intake.otel").info(
            "OpenTelemetry initialised: service=%s endpoint=%s", service_name, endpoint
        )
        return FastAPIInstrumentor  # caller will call instrument_app(web)

    except Exception as exc:  # noqa: BLE001
        logging.getLogger("file-intake.otel").warning(
            "OpenTelemetry init failed (tracing disabled): %s", exc
        )
        return None


_OTEL_FASTAPI_INSTRUMENTOR = _init_otel()


def _instrument_fastapi_app(app) -> None:  # type: ignore[no-untyped-def]
    """Attach OTel middleware to the FastAPI app instance.  Must be called
    AFTER ``web = FastAPI(...)`` since instrument_app() adds ASGI middleware."""
    if _OTEL_FASTAPI_INSTRUMENTOR is None:
        return
    try:
        _OTEL_FASTAPI_INSTRUMENTOR.instrument_app(app)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("file-intake.otel").warning(
            "FastAPI OTel instrumentation failed: %s", exc
        )

# ── Config ────────────────────────────────────────────────────────────────────
TG_TOKEN      = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED_IDS   = set(int(x) for x in os.getenv("TG_ALLOWED_USER_IDS", "").split(",") if x)
AUTH_TOKEN    = os.getenv("AUTH_TOKEN", "")
MAX_FILE_MB   = int(os.getenv("MAX_FILE_MB", "50"))

# ── Dispatcher ────────────────────────────────────────────────────────────────
AUDIO_MIMES = {"audio/mpeg", "audio/ogg", "audio/wav", "audio/mp4",
               "audio/x-m4a", "audio/aac", "video/ogg"}

def _get_tracer():  # type: ignore[return]
    try:
        from opentelemetry import trace
        return trace.get_tracer("file-intake")
    except Exception:  # noqa: BLE001
        return None

async def dispatch(data: bytes, mime: str, filename: str) -> dict:
    tracer = _get_tracer()
    span_ctx = tracer.start_as_current_span("dispatch", attributes={
        "file.mime": mime,
        "file.size_bytes": len(data),
        "file.name": filename,
    }) if tracer else _null_ctx()

    with span_ctx as span:
        if mime == "application/pdf":
            result = await pdf_handler.process(data, mime, filename)
        elif mime.startswith("image/") or filename.lower().endswith((".heic", ".heif")):
            result = await img_handler.process(data, mime or "image/jpeg", filename)
        elif mime in AUDIO_MIMES or filename.lower().endswith((".ogg", ".mp3", ".wav", ".m4a", ".aac")):
            result = await audio_handler.process(data, mime or "audio/ogg", filename)
        else:
            # fallback: try image
            result = await img_handler.process(data, "image/jpeg", filename)

        if tracer and span:
            span.set_attribute("result.type", result.get("type", "unknown"))
        return result


class _null_ctx:
    """No-op context manager used when OTel tracer is unavailable."""
    def __enter__(self): return None
    def __exit__(self, *a): pass

def make_title(result: dict, filename: str) -> str:
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    kind = {"image": "📷 Фото", "pdf": "📄 PDF", "audio": "🎙 Аудио"}.get(result["type"], "📎")
    return f"{kind}: {Path(filename).stem} [{now}]"

# ── Telegram bot ───────────────────────────────────────────────────────────────
def is_allowed(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    return uid in ALLOWED_IDS

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await update.message.reply_text(
        "Hello! Send me:\n"
        "📷 Photo — documents, receipts, images\n"
        "📎 PDF — reports, forms, documents\n"
        "🎙 Voice / audio — recordings or voice notes\n\n"
        "I will analyze and save the result."
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await update.message.reply_text(
        "/start — приветствие\n"
        "/help  — эта справка\n\n"
        "Просто отправь файл — определю тип автоматически."
    )

async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return

    msg = update.message
    processing_msg = await msg.reply_text("⏳ Обрабатываю...")

    try:
        # Determine file object and metadata
        tg_file = mime = filename = None

        if msg.photo:
            tg_file = await msg.photo[-1].get_file()
            mime, filename = "image/jpeg", "photo.jpg"
        elif msg.document:
            doc = msg.document
            tg_file = await doc.get_file()
            mime = doc.mime_type or "application/octet-stream"
            filename = doc.file_name or "document"
        elif msg.voice:
            tg_file = await msg.voice.get_file()
            mime, filename = "audio/ogg", "voice.ogg"
        elif msg.audio:
            aud = msg.audio
            tg_file = await aud.get_file()
            mime = aud.mime_type or "audio/mpeg"
            filename = aud.file_name or "audio.mp3"
        else:
            await processing_msg.edit_text("❓ Неподдерживаемый тип. Отправь фото, PDF или аудио.")
            return

        # Size check
        if tg_file.file_size and tg_file.file_size > MAX_FILE_MB * 1024 * 1024:
            await processing_msg.edit_text(f"❌ Файл > {MAX_FILE_MB} МБ. Уменьши и попробуй снова.")
            return

        # Download
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            await tg_file.download_to_drive(tmp.name)
            data = Path(tmp.name).read_bytes()
            Path(tmp.name).unlink(missing_ok=True)

        # Process
        result = await dispatch(data, mime, filename)
        answer  = result["text"]
        title   = make_title(result, filename)

        # Save to Trilium
        note_id = await trilium.save_note(title, answer, section=result.get("section", "notes"))
        if note_id:
            att_id = await trilium.attach_file(note_id, data, mime, filename)
            if att_id:
                await trilium.append_attachment_link(note_id, att_id, mime, filename)
        footer  = f"\n\n✅ Saved" if note_id else ""

        # Reply (Telegram message limit 4096 chars)
        if len(answer) <= 4000:
            await processing_msg.edit_text(answer + footer, parse_mode="Markdown")
        else:
            await processing_msg.edit_text(answer[:4000] + "…" + footer, parse_mode="Markdown")

    except Exception as e:
        await processing_msg.edit_text(f"❌ Ошибка: {e}")
        raise

# ── FastAPI web upload ─────────────────────────────────────────────────────────
web = FastAPI(title="file-intake")
_instrument_fastapi_app(web)  # attach OTel middleware (no-op if tracing disabled)

@web.get("/", response_class=HTMLResponse)
async def index():
    return open("/app/templates/index.html").read()

@web.post("/process")
async def process_web(
    file: UploadFile = File(...),
    x_auth_token: str = Header(default=""),
):
    if AUTH_TOKEN and x_auth_token != AUTH_TOKEN:
        raise HTTPException(403, "Unauthorized")
    if file.size and file.size > MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(413, f"File > {MAX_FILE_MB} MB")

    data     = await file.read()
    mime     = file.content_type or "application/octet-stream"
    filename = file.filename or "upload"

    try:
        result  = await dispatch(data, mime, filename)
        title   = make_title(result, filename)
        note_id = await trilium.save_note(title, result["text"], section=result.get("section", "notes"))
        if note_id:
            att_id = await trilium.attach_file(note_id, data, mime, filename)
            if att_id:
                await trilium.append_attachment_link(note_id, att_id, mime, filename)
        return {"text": result["text"], "note_id": note_id, "title": title}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        import logging, traceback
        logging.getLogger("file-intake").error("process failed: %s", e)
        traceback.print_exc()
        msg = str(e) or type(e).__name__
        if "authentication_error" in msg or "invalid x-api-key" in msg.lower() or "401" in msg:
            msg = "Claude API ключ недействителен (401). Проверь ANTHROPIC_API_KEY."
        return {"text": f"❌ Ошибка обработки: {msg[:300]}", "error": True}

@web.get("/health")
async def health():
    return {"status": "ok"}

# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    uvicorn.run(web, host="0.0.0.0", port=8090, log_level="warning")

if __name__ == "__main__":
    main()
