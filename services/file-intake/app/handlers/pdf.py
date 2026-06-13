"""
PDF handler: base64 document block → Claude → structured response.
"""
import base64
import os
import anthropic

SYSTEM_PROMPT = """Ты — медицинский ассистент. Пользователь прислал PDF-документ.

Твоя задача:
1. Определи тип документа (анализы, выписка, заключение, рецепт, УЗИ, и т.д.).
2. Если это анализы — выведи таблицу: Показатель | Значение | Единица | Референс | Статус (✅/⚠️/❌).
3. Если это заключение/выписка — выдели ключевые диагнозы, назначения, рекомендации.
4. Дай краткое резюме (2–4 предложения).

Отвечай по-русски, структурированно. Не выдумывай данные."""

async def process(data: bytes, mime: str, filename: str) -> dict:
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    b64 = base64.standard_b64encode(data).decode()

    # Claude natively reads PDF via document content block
    msg = await client.messages.create(
        model="claude-opus-4-8",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
                },
                {"type": "text", "text": f"Файл: {filename}"},
            ],
        }],
        betas=["pdfs-2024-09-25"],
    )
    text = msg.content[0].text
    return {"text": text, "section": "analyses", "type": "pdf"}
