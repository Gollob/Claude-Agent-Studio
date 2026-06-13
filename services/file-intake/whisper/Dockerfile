FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

ENV WHISPER_MODEL=small
ENV WHISPER_COMPUTE=int8
ENV WHISPER_LANGUAGE=ru

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8091"]
