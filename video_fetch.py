import os
import random
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

MOODS = {
    "dark": {
        "label": "🌑 Мрачный",
        "queries": ["dark aesthetic loop 1080p 60fps", "moody aesthetic dark edit background"],
    },
    "rain": {
        "label": "🌧️ Ночь и дождь",
        "queries": ["rain night aesthetic loop 1080p", "cyberpunk rain neon aesthetic video"],
    },
    "love": {
        "label": "💔 Тоска",
        "queries": ["anime sad aesthetic edit 1080p", "heartbreak aesthetic loop background"],
    },
    "calm": {
        "label": "🌤️ Спокойный",
        "queries": ["aesthetic clouds sky calm loop 1080p", "sunset aesthetic nature loop"],
    },
}


def _download_yt(query, outpath):
    cmd = [
        "yt-dlp",
        "--default-search", "ytsearch1:",
        "--format", "bv*[height<=1080][ext=mp4]/b[height<=1080][ext=mp4]/best[ext=mp4]/best",
        "--max-downloads", "1",
        "--no-playlist",
        "--output", outpath,
        query
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return os.path.exists(outpath) and os.path.getsize(outpath) > 50000
    except Exception:
        return False


def fetch(mood, need, cache_dir, gen_dir=None, progress=None):
    os.makedirs(cache_dir, exist_ok=True)
    queries = MOODS.get(mood, MOODS["dark"])["queries"]
    
    cached = sorted(f for f in os.listdir(cache_dir) if f.endswith(".mp4"))
    videos = [os.path.join(cache_dir, f) for f in cached]

    if len(videos) < 3:
        if progress:
            progress(None, "Ищу профессиональные видео-футажи…")
        for idx, q in enumerate(queries * 2):
            if len(videos) >= 4:
                break
            target = os.path.join(cache_dir, f"vid_{len(videos)+1:02d}.mp4")
            if os.path.exists(target) and os.path.getsize(target) > 50000:
                if target not in videos:
                    videos.append(target)
                continue
            if _download_yt(q, target):
                if target not in videos:
                    videos.append(target)

    if len(videos) < 2:
        # Fallback to procedural video generator if yt-dlp fails
        if progress:
            progress(None, "Генерирую фоновые видео...")
        fallback_vid = os.path.join(cache_dir, "fallback.mp4")
        if not os.path.exists(fallback_vid):
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                "color=c=black:s=1080x1920:r=30:d=10",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", fallback_vid
            ], capture_output=True)
        videos = [fallback_vid]

    return videos
