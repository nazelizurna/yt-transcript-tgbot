def fetch_transcript(video_id: str) -> Optional[list]:
    """
    Uses yt-dlp (instead of youtube_transcript_api) to pull captions.
    yt-dlp hits YouTube's innertube API rather than scraping the watch-page
    HTML directly, which tends to be more resilient to the IP-blocking that
    cloud providers (Render, AWS, etc.) run into with simpler scrapers.
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
            "en", "en-US", "en-GB", "ru", "es", "pt", "fr", "de",
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
        logger.error(f"Transcript fetch failed for {video_id}:\n{traceback.format_exc()}")
        return None
