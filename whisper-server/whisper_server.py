#!/usr/bin/env python3
"""
Faster-Whisper Server
---------------------
A lightweight HTTP server for speech-to-text transcription using faster-whisper.
Designed to run on a Mac Studio (or similar) and serve transcription requests
from the Scarecrow's Raspberry Pi over LAN.

Usage:
    pip install faster-whisper fastapi uvicorn python-multipart
    python whisper_server.py                    # Default: small model, port 8178
    python whisper_server.py --model base       # Use a different model
    python whisper_server.py --port 9000        # Use a different port

The server exposes a single endpoint:
    POST /transcribe  — accepts a WAV file upload, returns {"text": "..."}
"""

import io
import sys
import time
import wave
import argparse
import tempfile
import numpy as np

# --- Model loading (lazy, on first request) ---
_model = None
_model_name = "small"


def get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        print(f"[whisper-server] Loading model '{_model_name}'...")
        start = time.time()
        _model = WhisperModel(_model_name, compute_type="int8")
        elapsed = time.time() - start
        print(f"[whisper-server] Model loaded in {elapsed:.1f}s")
    return _model


# --- FastAPI app ---
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse

app = FastAPI(title="Faster-Whisper Server")


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    """Accept a WAV file and return the transcription."""
    start = time.time()

    try:
        # Read the uploaded file
        audio_bytes = await file.read()

        # Save to a temp file (faster-whisper needs a file path or numpy array)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        # Transcribe
        model = get_model()
        segments, info = model.transcribe(
            tmp_path,
            language="en",
            beam_size=5,
            vad_filter=True,  # Built-in VAD to skip silence
        )

        # Collect all segment texts
        text = " ".join(seg.text.strip() for seg in segments).strip()

        elapsed = time.time() - start

        # Clean up temp file
        import os
        os.unlink(tmp_path)

        print(f"[whisper-server] Transcribed ({elapsed:.3f}s): \"{text}\"")
        return {"text": text, "elapsed": round(elapsed, 3)}

    except Exception as e:
        elapsed = time.time() - start
        print(f"[whisper-server] Error ({elapsed:.3f}s): {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "model": _model_name}


# --- Main ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Faster-Whisper transcription server")
    parser.add_argument("--model", default="small",
                        help="Whisper model size: tiny, base, small, medium, large-v3 (default: small)")
    parser.add_argument("--port", type=int, default=8178,
                        help="Port to listen on (default: 8178)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Host to bind to (default: 0.0.0.0)")
    args = parser.parse_args()

    _model_name = args.model

    # Pre-load the model at startup
    get_model()

    import uvicorn
    print(f"[whisper-server] Listening on {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
