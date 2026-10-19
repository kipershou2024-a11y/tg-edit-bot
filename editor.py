import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

FPS = 30
W, H = 1080, 1920
PW, PH = 1620, 2880

_whisper_model = None


def run_ffmpeg(cmd, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + (r.stderr or "")[-1200:])


def probe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True)
    try:
        return float((r.stdout or "").strip())
    except ValueError:
        raise RuntimeError("не удалось прочитать аудио")


def extract_wav(src, wav):
    run_ffmpeg(["ffmpeg", "-y", "-i", src, "-ac", "1", "-ar", "44100", wav])
    return wav


def analyze(wav, duration):
    import librosa

    y, sr = librosa.load(wav, sr=44100, mono=True)
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
    beat_times = [float(t) for t in librosa.frames_to_time(beats, sr=sr) if t < duration - 0.3]
    oe = librosa.onset.onset_strength(y=y, sr=sr)
    energy = []
    prev = 0
    for bf in beats:
        seg = oe[prev:int(bf) + 1]
        energy.append(float(seg.mean()) if len(seg) else 0.0)
        prev = int(bf) + 1
    tempo = float(np.atleast_1d(tempo)[0])
    return beat_times, energy, tempo


def plan_cuts(beat_times, energy, duration):
    if not beat_times or duration <= 0:
        cuts = [0.0]
        t = 2.5
        while t < duration - 0.5:
            cuts.append(t)
            t += 2.5
        cuts.append(duration)
        return cuts

    thr = float(np.mean(energy) + 0.35 * np.std(energy)) if energy else 0.0
    n = len(beat_times)
    cuts = [0.0]
    i = 0
    while True:
        e = energy[i] if i < len(energy) else thr
        step = 2 if e > thr else 4
        j = i + step
        while j < n and beat_times[j] - cuts[-1] < 0.75:
            j += 1
        if j >= n or duration - beat_times[j] < 0.6:
            cuts.append(duration)
            break
        cuts.append(beat_times[j])
        i = j
    if cuts[1] - cuts[0] > 4.0:
        b = min(beat_times, key=lambda t: abs(t - 2.0))
        if 0.5 < b < cuts[1] - 0.5:
            cuts.insert(1, b)
    return cuts


def prep_images(files, outdir):
    from PIL import Image

    os.makedirs(outdir, exist_ok=True)
    out = []
    tr = 9 / 16
    for idx, f in enumerate(files):
        try:
            img = Image.open(f).convert("RGB")
        except Exception:
            continue
        w, h = img.size
        if w / h > tr:
            nw, nh = int(h * tr), h
            x0, y0 = (w - nw) // 2, 0
        else:
            nw, nh = w, int(w / tr)
            x0, y0 = 0, max(0, (h - nh) // 2)
        img = img.crop((x0, y0, x0 + nw, y0 + nh)).resize((PW, PH), Image.LANCZOS)
        p = os.path.join(outdir, f"p_{idx:02d}.png")
        img.save(p)
        out.append(p)
    return out


def build_segments(cuts, imgs, tmpdir, progress=None):
    files = []
    total = max(len(cuts) - 1, 1)
    tf = [int(round(c * FPS)) for c in cuts]
    for k in range(total):
        n = tf[k + 1] - tf[k]
        if n < 6:
            n = 6
        dur = n / FPS
        img = imgs[k % len(imgs)]
        out = os.path.join(tmpdir, f"seg_{k:03d}.mp4")
        p = k % 4
        if p == 0:
            zm = f"z='1+0.12*on/{n}':x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2'"
        elif p == 1:
            zm = f"z='1.12-0.12*on/{n}':x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2'"
        elif p == 2:
            zm = f"z='1.08':x='(iw-iw/zoom)*on/{n}':y='ih/2-(ih/zoom)/2'"
        else:
            zm = f"z='1.08':x='(iw-iw/zoom)*(1-on/{n})':y='ih/2-(ih/zoom)/2'"
        flash = ",fade=t=in:st=0:d=0.07:color=white" if dur <= 1.45 else ""
        vf = (f"zoompan={zm}:d={n}:s={W}x{H}:fps={FPS}{flash},format=yuv420p")
        run_ffmpeg(["ffmpeg", "-y", "-i", img, "-vf", vf, "-frames:v", str(n),
                    "-c:v", "libx264", "-crf", "16", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", out])
        files.append(out)
        if progress:
            progress(int(54 + 30 * (k + 1) / total), f"Сегмент {k + 1}/{total}")
    return files


def concat(segs, tmpdir):
    with open(os.path.join(tmpdir, "concat.txt"), "w", encoding="utf-8") as f:
        for s in segs:
            f.write(f"file '{os.path.basename(s)}'\n")
    out = os.path.join(tmpdir, "base.mp4")
    run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", "concat.txt", "-c", "copy", "base.mp4"], cwd=tmpdir)
    return out


def transcribe(wav, progress=None):
    global _whisper_model
    import whisper

    if _whisper_model is None:
        name = os.environ.get("WHISPER_MODEL", "small")
        if progress:
            progress(None, f"Загружаю модель Whisper ({name})…")
        _whisper_model = whisper.load_model(name)
    if progress:
        progress(None, "Распознаю текст песни…")
    return _whisper_model.transcribe(wav, language="ru", word_timestamps=True,
                                     initial_prompt="Текст песни, русский язык.")


def group_lines(result, max_chars=34, max_dur=3.2, max_gap=0.65):
    words = []
    for s in result.get("segments", []):
        for w in s.get("words", []):
            words.append((float(w["start"]), float(w["end"]), w["word"].strip()))
    lines, cur = [], []
    for w in words:
        if not cur:
            cur = [w]
            continue
        gap = w[0] - cur[-1][1]
        chars = sum(len(x[2]) + 1 for x in cur)
        dur = w[1] - cur[0][0]
        if gap > max_gap or chars + len(w[2]) > max_chars or dur > max_dur:
            lines.append(cur)
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(cur)
    return [(l[0][0], l[-1][1] + 0.15, " ".join(x[2] for x in l)) for l in lines]


def _ts(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def write_ass(lines, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,66,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,5,2,2,60,60,340,1
Style: Chorus,Impact,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,6,3,2,60,60,330,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""")
        for st, en, txt in lines:
            style = "Chorus" if (en - st) >= 2.6 else "Default"
            f.write(f"Dialogue: 0,{_ts(st)},{_ts(en)},{style},,0,0,0,,{{\\fad(100,90)}}{txt}\n")


def make_gradient(path):
    from PIL import Image

    g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    px = g.load()
    for y in range(H):
        a = 0 if y < 1360 else int(160 * (((y - 1360) / 560) ** 1.5))
        for x in range(W):
            px[x, y] = (0, 0, 0, a)
    g.save(path)


def finalize(base, audio_src, grad, has_ass, duration, out_path, tmpdir, progress=None):
    dur = max(duration, 1.0)
    budget = int(36 * 1024 * 8 / dur)
    mr = max(2500, min(10000, budget))
    fade = max(dur - 0.5, 0.1)
    grade = (f"[bg]eq=contrast=1.06:saturation=0.90:brightness=-0.015,"
             f"vignette=PI/5,noise=alls=3:allf=t,"
             f"fade=t=out:st={fade:.2f}:d=0.5[v0]")
    tail = "[v0]ass=subs.ass[v]" if has_ass else "[v0]null[v]"
    fc = f"[0:v][1:v]overlay=0:0[bg];{grade};{tail}"
    if progress:
        progress(92, "Финальная сборка…")
    run_ffmpeg(["ffmpeg", "-y", "-i", base, "-i", grad, "-i", audio_src,
                "-filter_complex", fc,
                "-map", "[v]", "-map", "2:a",
                "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
                "-c:v", "libx264", "-crf", "23", "-preset", "medium",
                "-maxrate", f"{mr}k", "-bufsize", f"{2 * mr}k",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", "-shortest", out_path], cwd=tmpdir)
    return out_path


def build(audio_src, job_dir, mood, with_lyrics, progress=None, pin_cache=None):
    progress = progress or (lambda p, s: None)
    tmp = os.path.join(job_dir, "tmp")
    os.makedirs(tmp, exist_ok=True)

    progress(3, "Готовлю аудио…")
    duration = probe_duration(audio_src)
    if duration < 3:
        raise RuntimeError("трек слишком короткий")
    if duration > 660:
        raise RuntimeError("трек длиннее 11 минут — не потяну")
    wav = extract_wav(audio_src, os.path.join(tmp, "track.wav"))

    progress(6, "Анализирую биты…")
    beats, energy, tempo = analyze(wav, duration)
    cuts = plan_cuts(beats, energy, duration)

    ass_path = os.path.join(tmp, "subs.ass")
    has_ass = False
    if with_lyrics:
        progress(14, "Распознаю текст песни…")
        res = transcribe(wav, progress)
        lines = group_lines(res)
        if lines:
            write_ass(lines, ass_path)
            has_ass = True
        progress(36, "Текст готов")
    else:
        progress(36, "Биты посчитаны")

    progress(38, "Ищу картинки…")
    cache = pin_cache or os.path.join(job_dir, "pins")
    files = __import__("pinterest_fetch").fetch(mood, 16, cache, job_dir, progress)
    if len(files) < 4:
        raise RuntimeError("не удалось собрать картинки")

    progress(50, "Кадрирую картинки…")
    imgs = prep_images(files, os.path.join(tmp, "prepared"))
    if len(imgs) < 4:
        raise RuntimeError("не удалось подготовить картинки")

    segs = build_segments(cuts, imgs, tmp, progress)

    progress(85, "Склеиваю…")
    base = concat(segs, tmp)
    grad = os.path.join(tmp, "gradient.png")
    make_gradient(grad)

    out_path = os.path.join(job_dir, "edit.mp4")
    finalize(base, audio_src, grad, has_ass, duration, out_path, tmp, progress)
    progress(100, "Готово")
    return {"out": out_path, "tempo": tempo, "duration": duration}
