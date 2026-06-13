"""
Image handler: JPEG/PNG/HEIC → Claude Vision → structured response.
"""
import base64
import os
from io import BytesIO
from pathlib import Path
import anthropic

def _encode(data: bytes, mime: str) -> tuple[str, str]:
    """Return (base64_data, media_type). Converts HEIC→JPEG if needed."""
    if mime in ("image/heic", "image/heif") or not mime.startswith("image/"):
        from PIL import Image
        import pillow_heif
        pillow_heif.register_heif_opener()
        img = Image.open(BytesIO(data)).convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=92)
        data = buf.getvalue()
        mime = "image/jpeg"
    return base64.standard_b64encode(data).decode(), mime

SYSTEM_PROMPT = """Ты — медицинский ассистент. Пользователь прислал фото медицинского документа.

Твоя задача:
1. Распознай все данные с изображения.
2. Если это анализы — выведи таблицу: Показатель | Значение | Единица | Референс | Статус (✅/⚠️/❌).
3. Дай краткую интерпретацию отклонений.
4. Если это не медицинский документ — опиши что на изображении.

Отвечай по-русски, структурированно. Не выдумывай значения."""

async def process(data: bytes, mime: str, filename: str) -> dict:
    b64, media_type = _encode(data, mime)
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    msg = await client.messages.create(
        model="claude-opus-4-8",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                },
                {"type": "text", "text": f"Файл: {filename}"},
            ],
        }],
    )
    text = msg.content[0].text
    return {"text": text, "section": "analyses", "type": "image"}
