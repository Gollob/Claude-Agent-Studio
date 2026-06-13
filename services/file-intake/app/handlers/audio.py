"""
Audio handler: voice/audio → Whisper transcription → Claude structure.
"""
import os
import httpx
import anthropic

WHISPER_URL = os.getenv("WHISPER_URL", "http://whisper:8091")

SYSTEM_PROMPT = """Ты — медицинский ассистент. Пользователь прислал аудиозапись или голосовое сообщение.

Тебе передаётся транскрипция. Твоя задача:
1. Если это запись приёма врача — выдели: диагноз, назначения, рекомендации, дату.
2. Если это голосовая заметка пациента — структурируй: симптомы, показатели (АД, пульс, температура и т.д.), дату.
3. Если это просто разговор — дай краткое резюме.

Отвечай по-русски. Не добавляй данных которых нет в тексте."""

async def process(data: bytes, mime: str, filename: str) -> dict:
    # Step 1: transcribe via Whisper (degrade gracefully on timeout/error)
    try:
        async with httpx.AsyncClient(timeout=120) as http:
            r = await http.post(
                f"{WHISPER_URL}/transcribe",
                files={"file": (filename, data, mime)},
            )
            r.raise_for_status()
            whisper = r.json()
    except Exception as exc:
        import logging
        err_msg = str(exc) or type(exc).__name__
        logging.getLogger("file-intake.audio").error(
            "Whisper unavailable (type=%s): %s", type(exc).__name__, exc
        )
        return {
            "text": f"⚠️ Whisper недоступен: {err_msg}. Попробуй позже.",
            "section": "notes",
            "type": "audio",
        }

    transcript = whisper.get("text", "").strip()
    duration   = whisper.get("duration", 0)

    if not transcript:
        return {
            "text": "⚠️ Не удалось распознать аудио. Проверь качество записи.",
            "section": "notes",
            "type": "audio",
        }

    # Step 2: structure via Claude (optional — degrade gracefully on any error)
    try:
        client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        msg = await client.messages.create(
            model="claude-opus-4-8",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": (
                    f"Длительность аудио: {duration:.0f} с.\n\n"
                    f"Транскрипция:\n{transcript}"
                ),
            }],
        )
        structured = msg.content[0].text
        text = f"🎙 Транскрипция ({duration:.0f} с):\n_{transcript}_\n\n{structured}"
        return {"text": text, "section": "notes", "type": "audio", "transcript": transcript}
    except Exception as exc:
        import logging
        logging.getLogger("file-intake.audio").error(
            "Claude structuring failed (type=%s): %s", type(exc).__name__, exc
        )
        return {
            "text": (
                f"🎙 Транскрипция ({duration:.0f} с):\n_{transcript}_\n\n"
                "⚠️ Структурирование пропущено (Claude недоступен)."
            ),
            "section": "notes",
            "type": "audio",
            "transcript": transcript,
        }
