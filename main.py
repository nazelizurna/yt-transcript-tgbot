"""
YouTube Transcript -> Telegram Document Bot (Webhook Architecture)
====================================================================
Deploy target: Render (native ASGI) or Vercel (via Mangum adapter, see notes at bottom).

Env vars required:
    TELEGRAM_BOT_TOKEN   - token from @BotFather
    WEBHOOK_SECRET       - (optional but recommended) random string used as the
                            URL path segment so randos on the internet can't POST to your bot

Dependencies (requirements.txt):
    fastapi
    uvicorn
    requests
    python-docx
    mangum          # only needed for Vercel deployment
"""

import os
import re
import tempfile
import logging
from typing import Optional

import requests
from fastapi import FastAPI, Request, HTTPException
from docx import Document
from docx.shared import Pt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("yt-transcript-bot")

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")  # empty string = no secret check
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"

# ---------------------------------------------------------------------------
# 3.2  URL Parsing & Regular Expressions
# ---------------------------------------------------------------------------
YOUTUBE_ID_PATTERNS = [
    r"(?:youtube\.com/watch\?v=)([A-Za-z0-9_-]{11})",
    r"(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
    r"(?:youtube\.com/embed/)([A-Za-z0-9_-]{11})",
    r"(?:youtu\.be/)([A-Za-z0-9_-]{11})",
    r"(?:m\.youtube\.com/watch\?v=)([A-Za-z0-9_-]{11})",
]


def extract_video_id(text: str) -> Optional[str]:
    """Return the 11-char YouTube video ID from a URL, or None if not found."""
    if not text:
        return None
    for pattern in YOUTUBE_ID_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Telegram helpers (raw requests, no wrapper framework)
# ---------------------------------------------------------------------------
def tg_call(method: str, **kwargs):
    """Generic Telegram Bot API call. Raises on missing token per spec 4."""
    if not TELEGRAM_BOT_TOKEN:
        # Spec 4: missing token -> safe 500
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN not configured")
    url = f"{TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN)}/{method}"
    files = kwargs.pop("files", None)
    resp = requests.post(url, data=kwargs, files=files, timeout=30)
    resp.raise_for_status()
    return resp.json()


def send_message(chat_id: int, text: str) -> int:
    """Send a text message, return its message_id."""
    result = tg_call("sendMessage", chat_id=chat_id, text=text)
    return result["result"]["message_id"]


def delete_message(chat_id: int, message_id: int) -> None:
    try:
        tg_call("deleteMessage", chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning(f"Could not delete status message: {e}")


def send_document(chat_id: int, file_path: str, caption: str) -> None:
    with open(file_path, "rb") as f:
        tg_call("sendDocument", chat_id=chat_id, caption=caption, files={"document": f})


# ---------------------------------------------------------------------------
# 3.3  Transcript retrieval
# ---------------------------------------------------------------------------
def fetch_transcript(video_id: str) -> Optional[list]:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        transcript = YouTubeTranscriptApi().fetch(video_id)

        return [
            {
                "start": segment.start,
                "text": segment.text
            }
            for segment in transcript
        ]

    except Exception as e:
        logger.exception(f"Transcript fetch failed for {video_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# 3.4  Document generation
# ---------------------------------------------------------------------------
def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"[{h:02d}:{m:02d}:{s:02d}]"
    return f"[{m:02d}:{s:02d}]"


def build_docx(video_id: str, segments: list) -> str:
    doc = Document()
    doc.add_heading(f"YouTube Transcript - Video {video_id}", level=1)

    for seg in segments:
        p = doc.add_paragraph()
        ts_run = p.add_run(format_timestamp(seg["start"]) + " ")
        ts_run.bold = True
        ts_run.font.size = Pt(11)
        text_run = p.add_run(seg["text"])
        text_run.font.size = Pt(11)

    fd, path = tempfile.mkstemp(suffix=".docx", dir="/tmp", prefix=f"{video_id}_")
    os.close(fd)
    doc.save(path)
    return path


# ---------------------------------------------------------------------------
# Core flow (3.1)
# ---------------------------------------------------------------------------
def process_update(update: dict) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return  # ignore non-message updates (e.g. callback_query)

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    video_id = extract_video_id(text)
    if not video_id:
        send_message(chat_id, "❌ Please send a valid YouTube link.")
        return

    status_id = send_message(chat_id, "⏳ Hook triggered! Extracting video transcript...")

    file_path = None
    try:
        segments = fetch_transcript(video_id)
        if not segments:
            send_message(chat_id, "❌ Transcript unavailable for this video context.")
            return

        file_path = build_docx(video_id, segments)
        send_document(chat_id, file_path, caption=f"📄 File ready for Video ID: {video_id}")
    finally:
        delete_message(chat_id, status_id)
        if file_path and os.path.exists(file_path):
            os.remove(file_path)  # 5. Data Isolation - purge temp file


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------
@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN not configured")

    update = await request.json()
    try:
        process_update(update)
    except HTTPException:
        raise
    except Exception as e:
        # Don't let a bad update crash the webhook — log and 200 back to Telegram
        logger.exception(f"Error processing update: {e}")

    return {"ok": True}


@app.get("/")
async def health_check():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Local dev entrypoint (Render uses this via `uvicorn main:app`)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))


# ---------------------------------------------------------------------------
# VERCEL NOTE
# ---------------------------------------------------------------------------
# Vercel's Python runtime expects an ASGI-compatible `handler`. Add this to a
# separate file (e.g. api/index.py) instead of running uvicorn directly:
#
#   from mangum import Mangum
#   from main import app
#   handler = Mangum(app)
#
# And a vercel.json routing all traffic to that file:
#   {
#     "builds": [{"src": "api/index.py", "use": "@vercel/python"}],
#     "routes": [{"src": "/(.*)", "dest": "api/index.py"}]
#   }
#
# Render is simpler: just set the start command to
#   uvicorn main:app --host 0.0.0.0 --port $PORT
# and it works with the file as-is (no Mangum needed).
