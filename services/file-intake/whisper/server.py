"""
Whisper transcription microservice.
POST /transcribe  — multipart: file (audio)
Returns: {"text": "...", "language": "ru", "duration": 12.3}
"""
import os
import tempfile
import subprocess
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException
import aiofiles
from faster_whisper import WhisperModel

MODEL_SIZE = os.getenv("WHISPER_MODEL", "small")
COMPUTE    = os.getenv("WHISPER_COMPUTE", "int8")
LANGUAGE   = os.getenv("WHISPER_LANGUAGE", "ru")

print(f"Loading Whisper model '{MODEL_SIZE}' ({COMPUTE})…", flush=True)
model = WhisperModel(MODEL_SIZE, device="cpu", compute_type=COMPUTE)
print("Whisper ready.", flush=True)

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_SIZE}

@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    suffix = Path(file.filename or "audio.ogg").suffix or ".ogg"
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, f"input{suffix}")
        wav = os.path.join(tmp, "audio.wav")

        async with aiofiles.open(src, "wb") as f:
            await f.write(await file.read())

        # Convert to 16kHz mono WAV
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-ar", "16000", "-ac", "1", wav],
            capture_output=True, timeout=120
        )
        if result.returncode != 0:
            raise HTTPException(500, f"ffmpeg error: {result.stderr.decode()[:300]}")

        segments, info = model.transcribe(wav, language=LANGUAGE, beam_size=5)
        text = " ".join(s.text.strip() for s in segments).strip()

    return {
        "text": text,
        "language": info.language,
        "duration": round(info.duration, 1),
    }
