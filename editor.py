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
        raise RuntimeError("Failed to parse duration")


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

        max_start = max(0.0, v_len - dur - 0.1)
        start_t = random.uniform(0.0, max_start) if max_start > 0 else 0.0

        out = os.path.join(tmpdir, "seg_{0:03d}.mp4".format(k))
        vf = (
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,"
            "eq=contrast=1.08:saturation=1.15:brightness=-0.02,"
            "vignette=PI/4,"
            "noise=alls=4:allf=t"
        )
        if dur <= 1.35:
            vf += ",fade=t=in:st=0:d=0.06:color=white"
        cmd = [
            "ffmpeg", "-y",
            "-ss", "{0:.2f}".format(start_t),
            "-i", src_v,
            "-t", "{0:.2f}".format(dur),
            "-vf", vf,
            "-r", str(FPS),
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-an", "-pix_fmt", "yuv420p",
            out
        ]
        run_ffmpeg(cmd)
        files.append(out)
        if progress:
            progress(int(50 + 35 * (k + 1) / total), "Montage seg {0}/{1}".format(k + 1, total))
    return files


def concat(segs, tmpdir):
    with open(os.path.join(tmpdir, "concat.txt"), "w", encoding="utf-8") as f:
        for s in segs:
            f.write("file '{0}'\n".format(os.path.basename(s)))
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
            progress(None, "Loading Whisper {0}:".format(name))
        _whisper_model = whisper.load_model(name)
    if progress:
        progress(None, "Transcribing audio:")
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
    return "{0}:{1:02d}:{2:05.2f}".format(h, m, s)


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
Style: Default,Impact,74,&H00FFFF00,&H00FF0000,&H000000CC,&H96000000,-1,0,0,0,100,100,0,0,1,4,3,2,60,60,340,1
Style: Chorus,Impact,100,&H00FFFFFF,&H00800000,&H000000CC,&H96000000,-1,0,0,0,100,100,0,0,1,5,5,2,60,60,330,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""")
        for st, en, txt in lines:
            style = "Chorus" if (en - st) >= 2.4 else "Default"
            f.write("Dialogue: 0,{0},{1},{2},,0,0,0,,{{\\fad(80,80)}}{3}\n".format(_ts(st), _ts(en), style, txt))


def make_gradient(path):
    from PIL import Image

    g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    px = g.load()
    for y in range(H):
        t = max(0, (y - 1400) / 520)
        alpha = int(90 * t ** 1.6)
        for x in range(W):
            px[x, y] = (0, 0, 0, alpha)
    g.save(path)


def _render_final(base, audio_src, grad, has_ass, duration, out_path, tmpdir, target_mb):
    dur = max(duration, 1.0)
    limit_bytes = int(target_mb * 1000 * 1000)
    target_kbps = max(2500, int(limit_bytes * 8 / dur / 1000))
    mr = min(target_kbps, 7500)
    buf = int(mr * 2)
    fade = max(dur - 0.4, 0.1)
    grade = ("[bg]eq=contrast=1.03:saturation=1.08,brightness=0.02,"
             "fade=t=out:st={0:.2f}:d=0.4[v0]".format(fade))
    tail = "[v0]ass=subs.ass[v]" if has_ass else "[v0]null[v]"
    fc = "[0:v][1:v]overlay=0:0[bg];{0};{1}".format(grade, tail)
    run_ffmpeg(["ffmpeg", "-y", "-i", base, "-i", grad, "-i", audio_src,
                "-filter_complex", fc,
                "-map", "[v]", "-map", "2:a",
                "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
                "-c:v", "libx264", "-crf", "22", "-preset", "medium",
                "-maxrate", "{0}k".format(mr), "-bufsize", "{0}k".format(buf),
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", "-shortest", out_path], cwd=tmpdir)


def finalize(base, audio_src, grad, has_ass, duration, out_path, tmpdir, progress=None, target_mb=46):
    if progress:
        progress(92, "Final render")
    _render_final(base, audio_src, grad, has_ass, duration, out_path, tmpdir, target_mb)
    return out_path


def split_video(path, tmpdir, part_bytes=47000000):
    if not os.path.exists(path):
        return [path]
    size = os.path.getsize(path)
    if size <= part_bytes:
        return [path]
    dur = probe_duration(path)
    n = (size + part_bytes - 1) // part_bytes
    n = max(2, min(int(n), 8))
    chunk = dur / n
    outs = []
    for i in range(n):
        ss = i * chunk
        to = dur if i == n - 1 else (i + 1) * chunk
        outp = os.path.join(tmpdir, "part_{0:02d}.mp4".format(i + 1))
        run_ffmpeg(["ffmpeg", "-y", "-ss", "{0:.3f}".format(ss),
                    "-to", "{0:.3f}".format(to),
                    "-i", path, "-c", "copy",
                    "-reset_timestamps", "1", "-movflags", "+faststart",
                    outp], cwd=tmpdir)
        if os.path.exists(outp):
            outs.append(outp)
    return outs if outs else [path]


def build(audio_src, job_dir, mood, with_lyrics, progress=None, pin_cache=None):
    progress = progress or (lambda p, s: None)
    tmp = os.path.join(job_dir, "tmp")
    os.makedirs(tmp, exist_ok=True)

    progress(3, "Подготовка аудио")
    duration = probe_duration(audio_src)
    if duration < 3:
        raise RuntimeError("Аудио слишком короткое")
    if duration > 660:
        raise RuntimeError("Аудио длиннее 11 минут")
    wav = extract_wav(audio_src, os.path.join(tmp, "track.wav"))

    progress(8, "Анализ битов")
    beats, energy, tempo = analyze(wav, duration)
    cuts = plan_cuts(beats, energy, duration)

    ass_path = os.path.join(tmp, "subs.ass")
    has_ass = False
    if with_lyrics:
        progress(18, "Распознавание текста")
        res = transcribe(wav, progress)
        lines = group_lines(res)
        if lines:
            write_ass(lines, ass_path)
            has_ass = True
        progress(38, "Текст готов")
    else:
        progress(38, "Без текста")

    progress(40, "Получение фото/видео с Pinterest и Google")
    cache = pin_cache or os.path.join(job_dir, "vids")
    import video_fetch
    media = video_fetch.fetch(mood, 5, cache, job_dir, progress)
    if not media:
        raise RuntimeError("Не удалось получить фото/видео для эдита")

    # Convert images into slow-zoom clips so stills feel like "edit"
    progress(46, "Подготовка исходников (CapCut-style)")
    videos = []
    for idx, f in enumerate(media):
        ext = os.path.splitext(f)[1].lower()
        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            zoomclip = os.path.join(tmp, "zoomclip_{0:02d}.mp4".format(idx))
            if not os.path.exists(zoomclip):
                frames = 10 * FPS
                vf = ("scale=1400:1820,setsar=1,"
                      "zoompan=z='1.0+0.12*on/{0}':d={1}:s=1080x1920:fps=30,"
                      "format=yuv420p".format(frames, frames))
                run_ffmpeg(["ffmpeg", "-y", "-loop", "1", "-i", f, "-vf", vf,
                            "-frames:v", str(frames), "-c:v", "libx264", "-crf", "16",
                            "-preset", "veryfast", "-r", str(FPS), "-an", "-pix_fmt", "yuv420p",
                            zoomclip])
            if os.path.exists(zoomclip):
                videos.append(zoomclip)
            continue
        videos.append(f)

    if not videos:
        raise RuntimeError("Не удалось подготовить исходники")

    progress(50, "Про-монтаж под биты")
    segs = build_segments(cuts, videos, tmp, progress)

    progress(85, "Склейка потока")
    base = concat(segs, tmp)
    grad = os.path.join(tmp, "gradient.png")
    make_gradient(grad)

    out_path = os.path.join(job_dir, "edit.mp4")
    target_mb = max(30, int(duration * 0.55))
    finalize(base, audio_src, grad, has_ass, duration, out_path, tmp, progress, target_mb)

    progress(96, "Разбивка при необходимости")
    parts = split_video(out_path, tmp, 47000000)
    progress(100, "Готово")
    return {"out": out_path, "outs": parts, "tempo": tempo, "duration": duration}
