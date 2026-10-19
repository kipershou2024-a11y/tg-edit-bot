import hashlib
import io
import os
import random
import re
import sys

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

MOODS = {
    "dark": {
        "label": "\U0001f311 Мрачный",
        "queries": ["dark aesthetic sad moody",
                    "black aesthetic heartbreak night",
                    "lonely aesthetic dark portrait"],
    },
    "rain": {
        "label": "\U0001f327️ Ночь и дождь",
        "queries": ["rain night city neon aesthetic",
                    "rain window moody aesthetic",
                    "night street aesthetic blue rain"],
    },
    "love": {
        "label": "\U0001f494 Тоска",
        "queries": ["love aesthetic sad couple",
                    "pink aesthetic heartbreak",
                    "vintage romantic aesthetic sad"],
    },
    "calm": {
        "label": "\U0001f324️ Спокойный",
        "queries": ["aesthetic sky clouds calm",
                    "minimal aesthetic landscape",
                    "pastel aesthetic nature soft"],
    },
}


def _scrape_urls(queries, need=30):
    from playwright.sync_api import sync_playwright

    urls = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        for q in queries:
            if len(urls) >= need:
                break
            try:
                page.goto("https://www.pinterest.com/search/pins/?q=" + q.replace(" ", "%20"),
                          wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3500)
                page.mouse.wheel(0, 3500)
                page.wait_for_timeout(2500)
                html = page.content()
            except Exception:
                continue
            for u in re.findall(r'https://i\.pinimg\.com/[^"\'\\]+?\.(?:jpg|jpeg|png|webp)', html):
                if any(x in u for x in ("75x75", "236x", "474x")):
                    continue
                if u not in urls:
                    urls.append(u)
        try:
            browser.close()
        except Exception:
            pass
    return urls


def _download(urls, outdir, limit):
    from PIL import Image

    os.makedirs(outdir, exist_ok=True)
    have = sorted(f for f in os.listdir(outdir) if f.startswith("img_"))
    if len(have) >= limit:
        return [os.path.join(outdir, f) for f in have[:limit]]

    seen = set()
    for f in have:
        try:
            seen.add(hashlib.md5(open(os.path.join(outdir, f), "rb").read()).hexdigest())
        except Exception:
            pass

    n = len(have)
    for u in urls:
        if n >= limit:
            break
        try:
            r = requests.get(u, headers={"User-Agent": UA}, timeout=15)
            if r.status_code != 200:
                continue
            h = hashlib.md5(r.content).hexdigest()
            if h in seen:
                continue
            img = Image.open(io.BytesIO(r.content))
            if min(img.size) < 480:
                continue
            seen.add(h)
            n += 1
            ext = (u.rsplit(".", 1)[-1] or "jpg").lower()
            if ext not in ("jpg", "jpeg", "png", "webp"):
                ext = "jpg"
            with open(os.path.join(outdir, f"img_{n:03d}.{ext}"), "wb") as fh:
                fh.write(r.content)
        except Exception:
            continue

    have = sorted(f for f in os.listdir(outdir) if f.startswith("img_"))
    return [os.path.join(outdir, f) for f in have[:limit]]


def procedural(outdir, count, size=(1620, 2880)):
    from PIL import Image, ImageDraw

    os.makedirs(outdir, exist_ok=True)
    pals = [
        ((12, 14, 34), (52, 26, 74), (4, 4, 10)),
        ((8, 22, 32), (14, 54, 72), (3, 8, 14)),
        ((26, 8, 16), (76, 28, 40), (10, 3, 7)),
        ((14, 18, 14), (36, 58, 40), (5, 9, 6)),
        ((30, 22, 10), (86, 60, 26), (12, 8, 4)),
    ]
    base = len([f for f in os.listdir(outdir) if f.startswith("gen_")])
    out = []
    for i in range(count):
        pal = random.choice(pals)
        top, mid, bot = pal
        img = Image.new("RGB", size, top)
        d = ImageDraw.Draw(img)
        h = size[1]
        for y in range(h):
            t = y / h
            if t < 0.5:
                k = t * 2
                c = tuple(int(top[j] + (mid[j] - top[j]) * k) for j in range(3))
            else:
                k = (t - 0.5) * 2
                c = tuple(int(mid[j] + (bot[j] - mid[j]) * k) for j in range(3))
            d.line([(0, y), (size[0], y)], fill=c)
        noise = Image.effect_noise(size, 42).convert("L").point(lambda v: int(v * 0.09))
        img.paste((255, 255, 255), (0, 0), noise)
        base += 1
        p = os.path.join(outdir, f"gen_{base:03d}.png")
        img.save(p)
        out.append(p)
    return out


def fetch(mood, need, cache_dir, gen_dir=None, progress=None):
    queries = MOODS.get(mood, MOODS["dark"])["queries"]
    files = []
    try:
        have = sorted(f for f in os.listdir(cache_dir) if f.startswith("img_")) if os.path.isdir(cache_dir) else []
        files = [os.path.join(cache_dir, f) for f in have[:need]]
    except Exception:
        files = []

    if len(files) < need:
        if progress:
            progress(None, "Ищу картинки на Pinterest…")
        try:
            urls = _scrape_urls(queries, need=need * 3)
        except Exception:
            urls = []
        files = _download(urls, cache_dir, need)

    if len(files) < need:
        target = gen_dir or cache_dir
        if progress:
            progress(None, "Дорисовываю фон…")
        files = files + procedural(target, need - len(files))
    return files[:need]
