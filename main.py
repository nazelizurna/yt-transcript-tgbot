import os
import re
import tempfile
import logging
import traceback
import time
from typing import Optional

import requests
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from docx import Document
from docx.shared import Pt, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("yt-transcript-bot")

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY")
# Confirm these two values against the exact code snippet shown on your
# RapidAPI endpoint page (Get Transcript -> Code Snippets -> Python -> Requests).
RAPIDAPI_HOST = os.environ.get("RAPIDAPI_HOST", "youtube-transcripts.p.rapidapi.com")
RAPIDAPI_URL = f"https://{RAPIDAPI_HOST}/youtube/transcript"

# --- Translation API (any OpenAI-compatible endpoint) ----------------------
# Defaults to Google Gemini's free tier. Get a key at https://aistudio.google.com/apikey
# Other options: DeepSeek  -> LLM_BASE_URL=https://api.deepseek.com , LLM_MODEL=deepseek-chat
#                Groq      -> LLM_BASE_URL=https://api.groq.com/openai/v1 , LLM_MODEL=llama-3.3-70b-versatile
LLM_API_KEY = os.environ.get("LLM_API_KEY")
LLM_BASE_URL = os.environ.get(
    "LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
).rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-2.5-flash")
TRANSLATE_CHUNK_CHARS = 10000

YOUTUBE_ID_PATTERNS = [
    r"(?:youtube\.com/watch\?v=)([A-Za-z0-9_-]{11})",
    r"(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
    r"(?:youtube\.com/embed/)([A-Za-z0-9_-]{11})",
    r"(?:youtu\.be/)([A-Za-z0-9_-]{11})",
    r"(?:m\.youtube\.com/watch\?v=)([A-Za-z0-9_-]{11})",
]

# --- Language handling ---------------------------------------------------

DEFAULT_LANG = "ru"

LANG_KEYWORDS = {
    "eng": "en",
    "english": "en",
    "en": "en",
    "fr": "fr",
    "french": "fr",
    "francais": "fr",
    "français": "fr",
}

# Simple in-memory cache, keyed by "<video_id>:<lang>". Resets on every
# restart/redeploy - fine for a low-traffic bot, but say the word if you
# want this backed by a small SQLite file instead so it survives restarts.
_transcript_cache = {}


def extract_video_id(text: str) -> Optional[str]:
    if not text:
        return None
    for pattern in YOUTUBE_ID_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def extract_lang_override(text: str, video_id: str) -> str:
    """Look for a trailing language keyword in the message, e.g.
    'https://youtu.be/FKiIzaHyIcs eng'. Defaults to Russian if none found."""
    remainder = text.replace(video_id, "").strip().lower()
    for keyword, lang_code in LANG_KEYWORDS.items():
        if re.search(rf"\b{keyword}\b", remainder):
            return lang_code
    return DEFAULT_LANG


# --- Telegram helpers ------------------------------------------------------

def tg_call(method: str, **kwargs):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")
    url = f"{TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN)}/{method}"
    files = kwargs.pop("files", None)
    resp = requests.post(url, data=kwargs, files=files, timeout=30)
    if not resp.ok:
        logger.error(f"Telegram API {method} failed: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    return resp.json()


def send_message(chat_id: int, text: str) -> Optional[int]:
    try:
        result = tg_call("sendMessage", chat_id=chat_id, text=text)
        return result["result"]["message_id"]
    except Exception:
        logger.error(f"send_message failed:\n{traceback.format_exc()}")
        return None


def delete_message(chat_id: int, message_id: int) -> None:
    try:
        tg_call("deleteMessage", chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning(f"Could not delete status message: {e}")


def send_document(chat_id: int, file_path: str, caption: str) -> None:
    with open(file_path, "rb") as f:
        tg_call("sendDocument", chat_id=chat_id, caption=caption, files={"document": f})


# --- Caption parsing (yt-dlp fallback path) --------------------------------

def _parse_vtt(vtt_text: str) -> list:
    """Parse a WEBVTT subtitle file into [{start, text}, ...] segments."""
    segments = []
    time_pattern = re.compile(r"(\d{2}:\d{2}:\d{2}\.\d{3}) --> ")
    tag_pattern = re.compile(r"<[^>]+>")

    current_start = None
    current_text_lines = []

    def flush():
        if current_start is not None and current_text_lines:
            text = " ".join(current_text_lines).strip()
            text = tag_pattern.sub("", text)
            if text:
                segments.append({"start": current_start, "text": text})

    for line in vtt_text.splitlines():
        match = time_pattern.match(line)
        if match:
            flush()
            h, m, s = match.group(1).split(":")
            current_start = int(h) * 3600 + int(m) * 60 + float(s)
            current_text_lines = []
        elif (
            line.strip()
            and not line.strip().isdigit()
            and "WEBVTT" not in line
            and "-->" not in line
        ):
            current_text_lines.append(line.strip())
    flush()
    return segments


def _parse_json3(data: dict) -> list:
    """Parse YouTube's json3 caption format into [{start, text}, ...] segments."""
    segments = []
    for event in data.get("events", []):
        if "segs" not in event:
            continue
        start = event.get("tStartMs", 0) / 1000.0
        text = "".join(seg.get("utf8", "") for seg in event["segs"]).strip()
        if text:
            segments.append({"start": start, "text": text})
    return segments


# --- Video metadata (title) ------------------------------------------------

def fetch_video_title(video_id: str) -> str:
    """
    Fetch the video's title via YouTube's public oEmbed endpoint (no API key
    required). Returns whatever title YouTube reports for the video - for
    Russian-language videos this is normally already in Russian. Falls back
    to a generic placeholder if the lookup fails for any reason.
    """
    try:
        resp = requests.get(
            "https://www.youtube.com/oembed",
            params={
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "format": "json",
            },
            timeout=10,
        )
        resp.raise_for_status()
        title = resp.json().get("title")
        if title:
            return title.strip()
    except Exception:
        logger.warning(f"Could not fetch title for {video_id}:\n{traceback.format_exc()}")
    return f"Видео {video_id}"


# --- Transcript sources ------------------------------------------------

def fetch_transcript_rapidapi(video_id: str, lang: str = DEFAULT_LANG) -> Optional[list]:
    """
    Primary transcript source: Supadata's YouTube Transcripts API via RapidAPI.
    Runs from Supadata's infrastructure, not Render's IP, so it sidesteps
    the datacenter-IP block yt-dlp hits directly.
    """
    if not RAPIDAPI_KEY:
        return None

    try:
        resp = requests.get(
            RAPIDAPI_URL,
            headers={
                "X-RapidAPI-Key": RAPIDAPI_KEY,
                "X-RapidAPI-Host": RAPIDAPI_HOST,
            },
            params={
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "lang": lang,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        # --- TEMP DEBUG ---
        logger.info(f"RapidAPI raw response keys: {list(data.keys())}")
        logger.info(f"RapidAPI response size: {len(resp.text)} chars")
        # ------------------

        # Supadata's response shape: {"lang": "en", "content": [{"text","offset","duration"}, ...]}
        raw_segments = data.get("content") or data.get("transcript") or []
        if not raw_segments:
            return None

        segments = [
            {"start": seg["offset"] / 1000.0, "text": seg["text"]}
            for seg in raw_segments
            if seg.get("text")
        ]

        # --- TEMP DEBUG ---
        total_chars = sum(len(s["text"]) for s in segments)
        last_ts = segments[-1]["start"] if segments else 0
        logger.info(f"Parsed {len(segments)} segments, {total_chars} total chars, last timestamp {last_ts:.1f}s")
        # ------------------

        return segments or None

    except Exception:
        logger.error(f"RapidAPI transcript fetch failed for {video_id} (lang={lang}):\n{traceback.format_exc()}")
        return None


def fetch_transcript_ytdlp(video_id: str) -> Optional[list]:
    """
    Fallback transcript source. Uses yt-dlp directly against YouTube, which
    is prone to being blocked from cloud IPs (Render, AWS, etc.) with
    'Sign in to confirm you're not a bot'. Does not support the language
    override - grabs whatever caption track is available per the
    preferred_langs order below.
    """
    try:
        import yt_dlp

        url = f"https://www.youtube.com/watch?v={video_id}"
        ydl_opts = {
            "quiet": False,
            "no_warnings": False,
            "verbose": True,
            "skip_download": True,
            "writeautomaticsub": True,
            "writesubtitles": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        manual_subs = info.get("subtitles") or {}
        auto_subs = info.get("automatic_captions") or {}

        preferred_langs = [
            "ru", "en", "en-US", "en-GB", "es", "pt", "fr", "de",
            "hi", "id", "ar", "ja", "ko", "zh-Hans", "zh-Hant", "it", "tr", "vi",
        ]

        track_list = None
        for lang in preferred_langs:
            if lang in manual_subs:
                track_list = manual_subs[lang]
                break
        if track_list is None:
            for lang in preferred_langs:
                if lang in auto_subs:
                    track_list = auto_subs[lang]
                    break
        if track_list is None:
            combined = {**auto_subs, **manual_subs}
            if combined:
                track_list = next(iter(combined.values()))

        if not track_list:
            logger.error(f"No caption tracks found for {video_id}")
            return None

        fmt_entry = next((f for f in track_list if f.get("ext") == "json3"), None)
        if fmt_entry is None:
            fmt_entry = next(
                (f for f in track_list if f.get("ext") == "vtt"), track_list[0]
            )

        resp = requests.get(fmt_entry["url"], timeout=30)
        resp.raise_for_status()

        if fmt_entry.get("ext") == "json3":
            segments = _parse_json3(resp.json())
        else:
            segments = _parse_vtt(resp.text)

        return segments or None

    except Exception:
        logger.error(f"yt-dlp transcript fetch failed for {video_id}:\n{traceback.format_exc()}")
        return None


def fetch_transcript(video_id: str, lang: str = DEFAULT_LANG) -> Optional[list]:
    cache_key = f"{video_id}:{lang}"
    if cache_key in _transcript_cache:
        return _transcript_cache[cache_key]

    segments = fetch_transcript_rapidapi(video_id, lang=lang)
    if not segments:
        logger.warning(f"RapidAPI transcript unavailable for {video_id} (lang={lang}), falling back to yt-dlp")
        segments = fetch_transcript_ytdlp(video_id)

    if segments:
        _transcript_cache[cache_key] = segments
    return segments


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"[{h:02d}:{m:02d}:{s:02d}]"
    return f"[{m:02d}:{s:02d}]"


def _add_page_number_field(paragraph):
    """Insert a PAGE field (auto page number) into a paragraph run."""
    run = paragraph.add_run()
    fldChar1 = OxmlElement('w:fldChar')
    fldChar1.set(qn('w:fldCharType'), 'begin')

    instrText = OxmlElement('w:instrText')
    instrText.set(qn('xml:space'), 'preserve')
    instrText.text = "PAGE"

    fldChar2 = OxmlElement('w:fldChar')
    fldChar2.set(qn('w:fldCharType'), 'end')

    run._r.append(fldChar1)
    run._r.append(instrText)
    run._r.append(fldChar2)


def build_docx(video_id: str, segments: list, video_title: str) -> str:
    doc = Document()

    # 0.5" margins all around
    for section in doc.sections:
        section.top_margin = Inches(0.5)
        section.bottom_margin = Inches(0.5)
        section.left_margin = Inches(0.5)
        section.right_margin = Inches(0.5)

        # Page number in bottom-right corner (footer)
        footer = section.footer
        footer_para = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
        footer_para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        footer_para.text = ""
        _add_page_number_field(footer_para)

    # Set default font to Times New Roman 14pt
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(14)
    # Ensure east-asian font mapping doesn't override it
    rPr = style.element.get_or_add_rPr()
    rFonts = rPr.find(qn('w:rFonts'))
    if rFonts is None:
        rFonts = OxmlElement('w:rFonts')
        rPr.append(rFonts)
    rFonts.set(qn('w:ascii'), 'Times New Roman')
    rFonts.set(qn('w:hAnsi'), 'Times New Roman')
    rFonts.set(qn('w:eastAsia'), 'Times New Roman')

    # Title line (centered, bold) - the real (Russian) video title, replacing
    # the old "YouTube Transcript - Video ..." heading.
    title_para = doc.add_paragraph()
    title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title_para.add_run(video_title)
    title_run.font.name = "Times New Roman"
    title_run.font.size = Pt(14)
    title_run.bold = True

    # Transcript body: one continuous paragraph, centered, no timestamps/line breaks
    full_text = " ".join(seg["text"].strip() for seg in segments if seg.get("text"))
    full_text = re.sub(r"\s+", " ", full_text).strip()

    body_para = doc.add_paragraph()
    body_para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    body_run = body_para.add_run(full_text)
    body_run.font.name = "Times New Roman"
    body_run.font.size = Pt(14)

    fd, path = tempfile.mkstemp(suffix=".docx", dir="/tmp", prefix=f"{video_id}_")
    os.close(fd)
    doc.save(path)
    return path


# --- English detection + translation to Russian ----------------------------

_ENGLISH_STOPWORDS = {
    "the", "and", "is", "are", "was", "were", "to", "of", "in", "that", "it",
    "you", "for", "with", "this", "have", "has", "be", "but", "not", "we",
    "they", "he", "she", "i", "my", "your", "so", "what", "can", "will",
    "just", "like", "there", "about", "if", "do", "going", "think", "know",
}


def is_english_text(text: str) -> bool:
    """Heuristic: mostly Latin letters AND a high share of common English words.
    Russian (Cyrillic) text and other Latin-script languages (e.g. French)
    return False."""
    sample = text[:20000]
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", sample))
    latin = len(re.findall(r"[A-Za-z]", sample))
    total_letters = cyrillic + latin
    if total_letters < 20:
        return False
    if cyrillic / total_letters > 0.3:
        return False
    words = re.findall(r"[a-z']+", sample.lower())
    if not words:
        return False
    hits = sum(1 for w in words if w in _ENGLISH_STOPWORDS)
    return hits / len(words) >= 0.15


def _split_text(text: str, max_chars: int = TRANSLATE_CHUNK_CHARS) -> list:
    """Split text into chunks <= max_chars, preferring sentence boundaries.
    Auto-captions often have no punctuation, so very long 'sentences' are
    split on word boundaries."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, current = [], ""
    for sentence in sentences:
        while len(sentence) > max_chars:
            cut = sentence.rfind(" ", 0, max_chars)
            if cut <= 0:
                cut = max_chars
            piece, sentence = sentence[:cut], sentence[cut:].lstrip()
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece)
        if not sentence:
            continue
        if current and len(current) + 1 + len(sentence) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


def _llm_translate_chunk(text: str, retries: int = 5) -> str:
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY not configured")

    last_error = None
    for attempt in range(retries):
        try:
            resp = requests.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={
                    "model": LLM_MODEL,
                    "temperature": 0.2,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a professional English-to-Russian translator. "
                                "Translate the user's text into natural, accurate Russian. "
                                "The text is a part of a video transcript. "
                                "Output ONLY the translation as plain continuous text, "
                                "with no comments, notes, headings or quotation marks added."
                            ),
                        },
                        {"role": "user", "content": text},
                    ],
                },
                timeout=120,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            if content and content.strip():
                return content.strip()
            raise RuntimeError("Empty translation returned")
        except Exception as e:
            last_error = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            # Rate limited (429): wait much longer before retrying
            wait = 20 * (attempt + 1) if status == 429 else 3 * (attempt + 1)
            logger.warning(f"Translation attempt {attempt + 1} failed: {e} (waiting {wait}s)")
            time.sleep(wait)
    raise last_error


def translate_to_russian(text: str) -> str:
    chunks = _split_text(text)
    logger.info(f"Translating {len(text)} chars in {len(chunks)} chunks")
    return " ".join(_llm_translate_chunk(chunk) for chunk in chunks)


def process_update(update: dict) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    video_id = extract_video_id(text)
    if not video_id:
        send_message(chat_id, "❌ Please send a valid YouTube link.")
        return

    lang = extract_lang_override(text, video_id)
    status_id = send_message(chat_id, "⏳ Extracting video transcript...")

    file_path = None
    try:
        segments = fetch_transcript(video_id, lang=lang)
        if not segments:
            send_message(chat_id, "❌ Transcript unavailable for this video.")
            return

        video_title = fetch_video_title(video_id)

        # If the transcript is English, translate it (and the title) to Russian.
        # Russian transcripts are left untouched.
        transcript_text = " ".join(seg["text"] for seg in segments if seg.get("text"))
        if is_english_text(transcript_text):
            try:
                translated = translate_to_russian(re.sub(r"\s+", " ", transcript_text).strip())
                segments = [{"start": segments[0]["start"], "text": translated}]
                if is_english_text(video_title):
                    video_title = translate_to_russian(video_title)
                lang = "en→ru"
            except Exception:
                logger.error(f"Translation failed for {video_id}:\n{traceback.format_exc()}")
                lang = "en, not translated"
                send_message(chat_id, "⚠️ Translation failed, sending the original English text.")

        file_path = build_docx(video_id, segments, video_title)
        send_document(chat_id, file_path, caption=f"📄 File ready for Video ID: {video_id} ({lang})")
    except Exception:
        logger.error(f"process_update failed for video {video_id}:\n{traceback.format_exc()}")
        send_message(chat_id, "❌ Something went wrong generating your transcript.")
    finally:
        if status_id:
            delete_message(chat_id, status_id)
        if file_path and os.path.exists(file_path):
            os.remove(file_path)


# Telegram re-sends an update if it doesn't get a reply quickly. Processing now
# runs in the background (reply is instant), and update_ids already seen are
# ignored as a second safety net against duplicates.
_seen_update_ids = set()


def _run_update(update: dict) -> None:
    try:
        process_update(update)
    except Exception:
        logger.error(f"Unhandled error in process_update:\n{traceback.format_exc()}")


@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request, background_tasks: BackgroundTasks):
    # Never 500 on webhook secret mismatch checks or config issues —
    # always return 200 so Telegram doesn't hammer retries, and log everything.
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        logger.warning(f"Rejected webhook call with bad secret: {secret}")
        return {"ok": True}

    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is NOT SET in this environment — check Render Environment tab.")
        return {"ok": True}

    try:
        update = await request.json()
    except Exception:
        logger.error(f"Failed to parse incoming update as JSON:\n{traceback.format_exc()}")
        return {"ok": True}

    update_id = update.get("update_id")
    if update_id is not None:
        if update_id in _seen_update_ids:
            logger.info(f"Ignoring duplicate update {update_id}")
            return {"ok": True}
        _seen_update_ids.add(update_id)
        if len(_seen_update_ids) > 1000:
            _seen_update_ids.clear()

    background_tasks.add_task(_run_update, update)
    return {"ok": True}


@app.get("/")
async def health_check():
    return {"status": "ok"}


@app.get("/debug")
async def debug():
    """Temporary: confirms whether env vars are actually loaded in this deployment."""
    return {
        "token_set": bool(TELEGRAM_BOT_TOKEN),
        "token_length": len(TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else 0,
        "webhook_secret_set": bool(WEBHOOK_SECRET),
        "rapidapi_key_set": bool(RAPIDAPI_KEY),
        "rapidapi_host": RAPIDAPI_HOST,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
