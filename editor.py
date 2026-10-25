import os
import random
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

_whisper_model = None


def run_ffmpeg(cmd, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg error: " + (r.stderr or "")[-1200:])


def probe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True)
    try:
        return float((r.stdout or "").strip())
    except ValueError:
        raise RuntimeError("Не удалось прочитать длительность аудио")


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
        t = 2.0
        while t < duration - 0.5:
            cuts.append(t)
            t += 2.0
        cuts.append(duration)
        return cuts

    thr = float(np.mean(energy) + 0.3 * np.std(energy)) if energy else 0.0
    n = len(beat_times)
    cuts = [0.0]
    i = 0
    while True:
        e = energy[i] if i < len(energy) else thr
        step = 1 if e > thr else 3
        j = i + step
        while j < n and beat_times[j] - cuts[-1] < 0.55:
            j += 1
        if j >= n or duration - beat_times[j] < 0.4:
            cuts.append(duration)
            break
        cuts.append(beat_times[j])
        i = j
    if cuts[1] - cuts[0] > 3.0:
        b = min(beat_times, key=lambda t: abs(t - 1.5))
        if 0.4 < b < cuts[1] - 0.4:
            cuts.insert(1, b)
    return cuts


def build_segments(cuts, videos, tmpdir, progress=None):
    files = []
    total = max(len(cuts) - 1, 1)
    tf = [int(round(c * FPS)) for c in cuts]

    # Get durations of source videos
    v_durs = {}
    for v in videos:
        try:
            v_durs[v] = probe_duration(v)
        except Exception:
            v_durs[v] = 10.0

    for k in range(total):
        n = tf[k + 1] - tf[k]
        if n < 5:
            n = 5
        dur = n / FPS
        
        src_v = videos[k % len(videos)]
        v_len = v_durs.get(src_v, 10.0)
        
        # Pick random start time in source video
        max_start = max(0.0, v_len - dur - 0.1)
        start_t = random.uniform(0.0, max_start) if max_start > 0 else 0.0

        out = os.path.join(tmpdir, f"seg_{k:03d}.mp4")
        
        # Pro editing video filters: crop/scale to 1080x1920, cinematic color grade, contrast, saturation, vignette, noise
        vf = (
            f"scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,"
            f"eq=contrast=1.08:saturation=1.15:brightness=-0.02,"
            f"vignette=PI/4,"
            f"noise=alls=4:allf=t"
        )
        if dur <= 1.35:
            vf += ",fade=t=in:st=0:d=0.06:color=white"

        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start_t:.2f}",
            "-i", src_v,
            "-t", f"{dur:.2f}",
            "-vf", vf,
            "-r", str(FPS),
            "-c:v", "libx264", "-crf", "16", "-preset", "veryfast",
            "-an", "-pix_fmt", "yuv420p",
            out
        ]
        run_ffmpeg(cmd)
        files.append(out)
        if progress:
            progress(int(50 + 35 * (k + 1) / total), f"Монтаж кадра {k + 1}/{total}")
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
            progress(None, f"Загружаю модель Whisper ({name}):")
        _whisper_model = whisper.load_model(name)
    if progress:
        progress(None, "Распознаю текст песни...")
    return _whisper_model.transcribe(wav, language="ru", word_timestamps=True,
                                     initial_prompt="Переведи текст песни.")


def group_lines(result, max_chars=28, max_dur=2.8, max_gap=0.5):
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
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, 
StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Impact,78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,6,3,2,60,60,340,1
Style: Chorus,Impact,92,&H00FFFF00,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,7,4,2,60,60,330,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""")
        for st, en, txt in lines:
            style = "Chorus" if (en - st) >= 2.4 else "Default"
            f.write('Dialogue: 0,{s},{e},{style},,0,0,0,,{{\\fad(80,80)}}{txt}\n'.format(s=_ts(st), e=_ts(en), style=style, txt=txt))


def make_gradient(path):
    from PIL import Image

    g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    px = g.load()
    for y in range(H):
        a = 0 if y < 1320 else int(180 * (((y - 1320) / 600) ** 1.5))
        for x in range(W):
            px[x, y] = (0, 0, 0, a)
    g.save(path)


def finalize(base, audio_src, grad, has_ass, duration, out_path, tmpdir, progress=None):
    dur = max(duration, 1.0)
    budget = int(38 * 1024 * 8 / dur)
    mr = max(3000, min(12000, budget))
    fade = max(dur - 0.5, 0.1)
    grade = (f"[bg]eq=contrast=1.04:saturation=1.05,"
             f"fade=t=out:st={fade:.2f}:d=0.5[v0]")
    tail = "[v0]ass=subs.ass[v]" if has_ass else "[v0]null[v]"
    fc = f"[0:v][1:v]overlay=0:0[bg];{grade};{tail}"
    if progress:
        progress(92, "Финальный рендеринг Pro...")
    run_ffmpeg(["ffmpeg", "-y", "-i", base, "-i", grad, "-i", audio_src,
                "-filter_complex", fc,
                "-map", "[v]", "-map", "2:a",
                "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
                "-c:v", "libx264", "-crf", "22", "-preset", "medium",
                "-maxrate", f"{mr}k", "-bufsize", f"{2 * mr}k",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", "-shortest", out_path], cwd=tmpdir)
    return out_path


def build(audio_src, job_dir, mood, with_lyrics, progress=None, pin_cache=None):
    progress = progress or (lambda p, s: None)
    tmp = os.path.join(job_dir, "tmp")
    os.makedirs(tmp, exist_ok=True)

    progress(3, "Подготовка аудио...")
    duration = probe_duration(audio_src)
    if duration < 3:
        raise RuntimeError("Аудио слишком короткое")
    if duration > 660:
        raise RuntimeError("Аудио длиннее 11 минут")
    wav = extract_wav(audio_src, os.path.join(tmp, "track.wav"))

    progress(8, "Анализ битов и кач-энергии...")
    beats, energy, tempo = analyze(wav, duration)
    cuts = plan_cuts(beats, energy, duration)

    ass_path = os.path.join(tmp, "subs.ass")
    has_ass = False
    if with_lyrics:
        progress(18, "Распознавание текста (Whisper)...")
        res = transcribe(wav, progress)
        lines = group_lines(res)
        if lines:
            write_ass(lines, ass_path)
            has_ass = True
        progress(38, "Текст готов")
    else:
        progress(38, "Без текста")

    progress(40, "Загрузка видео-футажей...")
    cache = pin_cache or os.path.join(job_dir, "vids")
    import video_fetch
    videos = video_fetch.fetch(mood, 4, cache, job_dir, progress)
    if not videos:
        raise RuntimeError("Не удалось получить видео-футажи")

    progress(50, "Про-монтаж под биты...")
    segs = build_segments(cuts, videos, tmp, progress)

    progress(85, "Склейка потока...")
    base = concat(segs, tmp)
    grad = os.path.join(tmp, "gradient.png")
    make_gradient(grad)

    out_path = os.path.join(job_dir, "edit.mp4")
    finalize(base, audio_src, grad, has_ass, duration, out_path, tmp, progress)
    progress(100, "Готово!")
    return {"out": out_path, "tempo": tempo, "duration": duration}
