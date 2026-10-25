#!/usr/bin/env python3
"""video_analyze.py - fetch a video, extract audio transcript + keyframes, write a manifest.

Subcommands:
  doctor                                                  check tools and transcription backends
  run <url-or-file> [--cookies-from-browser chrome] [--lang xx] [--backend B] [--model M]
                    [--scene 0.3] [--max-frames 40] [--no-audio]
  frame <id> <time> [--n 3] [--span 2.0] [--width 1280]   zoom on a moment
  slice <id> <start> <end> [--lang xx] [--backend B]      re-transcribe one segment
  list                                                    show analysed videos

Transcription backends (auto picks the first available):
  mlx             mlx_whisper CLI (Apple Silicon)
  faster-whisper  faster_whisper Python package (Linux/macOS/Windows, CPU or CUDA)
  whisper         openai-whisper CLI (any platform, slowest)
Override with --backend or VIDEO_ANALYZE_BACKEND; model with --model or VIDEO_ANALYZE_MODEL.

Work dir: ~/.video-analysis/<id>/   (override with VIDEO_ANALYSIS_HOME)
Needs: ffmpeg, ffprobe; yt-dlp for URLs; one transcription backend for audio.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ.get("VIDEO_ANALYSIS_HOME", Path.home() / ".video-analysis"))
BACKENDS = ("mlx", "faster-whisper", "whisper")
DEFAULT_MODELS = {
    "mlx": "mlx-community/whisper-large-v3-turbo",
    "faster-whisper": "large-v3-turbo",
    "whisper": "turbo",
}


def sh(cmd, check=True, capture=False):
    return subprocess.run(
        cmd, check=check, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def need(tool):
    if not shutil.which(tool):
        sys.exit(f"missing required tool: {tool} (run `doctor` for install hints)")


def fmt_ts(sec):
    sec = max(0.0, float(sec))
    return f"{int(sec // 3600):02d}:{int(sec % 3600 // 60):02d}:{sec % 60:06.3f}"


def parse_ts(s):
    """Accept seconds (12.5), mm:ss, or hh:mm:ss."""
    total = 0.0
    for p in str(s).split(":"):
        total = total * 60 + float(p)
    return total


# ---------------------------------------------------------------- backends

def is_apple_silicon():
    return sys.platform == "darwin" and platform.machine() == "arm64"


def backend_available(name):
    if name == "mlx":
        return shutil.which("mlx_whisper") is not None
    if name == "faster-whisper":
        return importlib.util.find_spec("faster_whisper") is not None
    if name == "whisper":
        return shutil.which("whisper") is not None
    return False


def pick_backend(requested):
    requested = requested or os.environ.get("VIDEO_ANALYZE_BACKEND") or "auto"
    if requested != "auto":
        if requested not in BACKENDS:
            sys.exit(f"unknown backend {requested}; choose from auto, {', '.join(BACKENDS)}")
        if not backend_available(requested):
            sys.exit(f"backend {requested} is not installed (run `doctor` for install hints)")
        return requested
    order = BACKENDS if is_apple_silicon() else ("faster-whisper", "whisper", "mlx")
    for name in order:
        if backend_available(name):
            return name
    sys.exit("no transcription backend found (run `doctor`), or rerun with --no-audio")


def pick_model(backend, requested):
    return requested or os.environ.get("VIDEO_ANALYZE_MODEL") or DEFAULT_MODELS[backend]


def _norm(segs):
    return [{"start": round(float(s["start"]), 2), "end": round(float(s["end"]), 2),
             "text": s["text"].strip()} for s in segs if s["text"].strip()]


def _transcribe_mlx(wav, out_dir, model, lang):
    name = f"_raw_{wav.stem}"
    cmd = ["mlx_whisper", str(wav), "--model", model, "-f", "json", "-o", str(out_dir),
           "--output-name", name, "--condition-on-previous-text", "False", "--verbose", "False"]
    if lang:
        cmd += ["--language", lang]
    sh(cmd)
    raw = out_dir / f"{name}.json"
    j = json.loads(raw.read_text())
    raw.unlink()
    return _norm(j.get("segments", [])), j.get("language")


def _transcribe_faster_whisper(wav, out_dir, model, lang):
    from faster_whisper import WhisperModel
    device, compute = "cpu", "int8"
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            device, compute = "cuda", "float16"
    except Exception:
        pass
    # Feed the 16 kHz mono wav ffmpeg already made as samples, bypassing faster-whisper's
    # PyAV decoder (breaks when PyAV and faster-whisper versions drift apart).
    import wave
    import numpy as np
    with wave.open(str(wav), "rb") as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    m = WhisperModel(model, device=device, compute_type=compute)
    segments, info = m.transcribe(audio, language=lang, vad_filter=True,
                                  condition_on_previous_text=False)
    segs = [{"start": s.start, "end": s.end, "text": s.text} for s in segments]
    return _norm(segs), info.language


def _transcribe_whisper(wav, out_dir, model, lang):
    tmp = out_dir / "_whisper_tmp"
    tmp.mkdir(exist_ok=True)
    cmd = ["whisper", str(wav), "--model", model, "--output_format", "json", "--output_dir", str(tmp),
           "--condition_on_previous_text", "False", "--verbose", "False"]
    if lang:
        cmd += ["--language", lang]
    sh(cmd)
    j = json.loads((tmp / f"{wav.stem}.json").read_text())
    shutil.rmtree(tmp)
    return _norm(j.get("segments", [])), j.get("language")


TRANSCRIBERS = {
    "mlx": _transcribe_mlx,
    "faster-whisper": _transcribe_faster_whisper,
    "whisper": _transcribe_whisper,
}


def looks_hallucinated(segs, duration):
    """Flag classic Whisper failure modes on music/silence: one token looped, or far more
    text than the audio could hold."""
    text = " ".join(s["text"] for s in segs)
    if not text:
        return False
    if re.search(r"(\w{2,12}?)[\s,.!?-]*(?:\1[\s,.!?-]*){9,}", text, re.IGNORECASE):
        return True
    words = len(text.split()) or len(text) / 6
    return duration > 0 and words / duration > 6  # speech rarely exceeds ~4 words/s


def write_transcript(segs, lang, wd, stem="transcript"):
    (wd / "segments.json" if stem == "transcript" else wd / f"{stem}.json").write_text(
        json.dumps(segs, ensure_ascii=False, indent=1))
    (wd / f"{stem}.txt").write_text("\n".join(s["text"] for s in segs) + "\n")
    if stem == "transcript":
        srt = []
        for i, s in enumerate(segs, 1):
            a, b = fmt_ts(s["start"]).replace(".", ","), fmt_ts(s["end"]).replace(".", ",")
            srt.append(f"{i}\n{a} --> {b}\n{s['text']}\n")
        (wd / "transcript.srt").write_text("\n".join(srt))
        (wd / "transcript.json").write_text(
            json.dumps({"language": lang, "segments": segs}, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- pipeline

def video_id(src):
    if re.match(r"^https?://", src):
        out = sh(["yt-dlp", "--no-warnings", "--print", "%(extractor)s-%(id)s", "--skip-download", src],
                 check=False, capture=True)
        vid = (out.stdout or "").strip().splitlines()
        if vid:
            return re.sub(r"[^A-Za-z0-9._-]", "_", vid[-1])
        return "url-" + hashlib.sha1(src.encode()).hexdigest()[:10]
    p = Path(src).expanduser().resolve()
    return "file-" + hashlib.sha1(str(p).encode()).hexdigest()[:10]


def probe(path):
    out = sh(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
             capture=True)
    j = json.loads(out.stdout)
    v = next((s for s in j["streams"] if s["codec_type"] == "video"), {})
    a = next((s for s in j["streams"] if s["codec_type"] == "audio"), None)
    return {
        "duration": float(j["format"].get("duration", 0) or 0),
        "width": v.get("width"), "height": v.get("height"),
        "has_audio": a is not None,
        "audio_codec": a.get("codec_name") if a else None,
    }


def fetch(src, wd, cookies):
    existing = next((p for p in sorted(wd.glob("video.*")) if not p.name.endswith(".json")), None)
    if existing:
        return existing, {}
    if re.match(r"^https?://", src):
        need("yt-dlp")
        cmd = ["yt-dlp", "--no-warnings", "--write-info-json", "--no-playlist",
               "-f", "bv*[height<=720]+ba/b[height<=720]/b",
               "--merge-output-format", "mp4",
               "-o", str(wd / "video.%(ext)s"), src]
        if cookies:
            cmd[1:1] = ["--cookies-from-browser", cookies]
        sh(cmd)
        info = {}
        ij = next(iter(wd.glob("video.info.json")), None)
        if ij:
            raw = json.loads(ij.read_text())
            info = {k: raw.get(k) for k in ("title", "uploader", "channel", "description", "upload_date",
                                            "view_count", "like_count", "webpage_url", "extractor")}
        vf = next(p for p in sorted(wd.glob("video.*")) if not p.name.endswith(".json"))
        return vf, info
    p = Path(src).expanduser().resolve()
    if not p.exists():
        sys.exit(f"file not found: {p}")
    dest = wd / ("video" + p.suffix)
    if not dest.exists():
        try:
            dest.symlink_to(p)
        except OSError:
            shutil.copy2(p, dest)
    return dest, {"title": p.name}


def extract_audio(video, wd):
    wav = wd / "audio.wav"
    if not wav.exists():
        sh(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    return wav


def keyframes(video, wd, duration, scene, max_frames):
    fdir = wd / "frames"
    if fdir.exists():
        shutil.rmtree(fdir)
    fdir.mkdir()
    log = wd / "showinfo.log"
    with open(log, "w") as lf:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "info", "-i", str(video), "-vf",
             f"select='gt(scene,{scene})+eq(n,0)',scale=768:-2,showinfo",
             "-vsync", "vfr", "-q:v", "3", str(fdir / "f_%04d.jpg")],
            stderr=lf, check=True)
    times = [float(m.group(1)) for m in re.finditer(r"pts_time:([0-9.]+)", log.read_text())]
    files = sorted(fdir.glob("f_*.jpg"))
    times = times[:len(files)]
    # too few: add an evenly spaced fallback set so long static videos are still covered
    if len(files) < 8 and duration > 0:
        n = 12
        for i in range(n):
            t = duration * (i + 0.5) / n
            out = fdir / f"i_{i:04d}.jpg"
            sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                "-vf", "scale=768:-2", "-q:v", "3", str(out)])
            files.append(out)
            times.append(t)
    pairs = sorted(zip(times, files), key=lambda x: x[0])
    deduped = []
    for t, f in pairs:  # drop frames closer than 0.5s to the previous kept one
        if deduped and t - deduped[-1][0] < 0.5:
            f.unlink()
        else:
            deduped.append((t, f))
    pairs = deduped
    if len(pairs) > max_frames:  # even subsample, keep first and last
        step = (len(pairs) - 1) / (max_frames - 1)
        keep = {round(i * step) for i in range(max_frames)}
        for i, (_, f) in enumerate(pairs):
            if i not in keep:
                f.unlink()
        pairs = [p for i, p in enumerate(pairs) if i in keep]
    frames = []
    for i, (t, f) in enumerate(pairs, 1):
        new = fdir / f"k_{i:03d}_{int(t):05d}s.jpg"
        f.rename(new)
        frames.append({"index": i, "time": round(t, 2), "ts": fmt_ts(t), "file": str(new.relative_to(wd))})
    # contact sheets, 6x6 per sheet, cell order == frames order
    sheets = []
    if frames:
        sdir = wd / "sheets"
        if sdir.exists():
            shutil.rmtree(sdir)
        sdir.mkdir()
        lst = wd / "frames.txt"
        lst.write_text("".join(f"file '{wd / fr['file']}'\n" for fr in frames))
        sh(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-vf",
            "scale=320:-2,tile=6x6", "-vsync", "0", str(sdir / "sheet_%02d.jpg")])
        sheets = [str(p.relative_to(wd)) for p in sorted(sdir.glob("sheet_*.jpg"))]
        lst.unlink()
    log.unlink()
    return frames, sheets


# ---------------------------------------------------------------- commands

def cmd_doctor(_):
    linux = sys.platform.startswith("linux")
    print(f"platform: {sys.platform} {platform.machine()}  python {platform.python_version()}")
    hints = {
        "ffmpeg": "brew install ffmpeg" if not linux else "sudo apt install ffmpeg",
        "ffprobe": "ships with ffmpeg",
        "yt-dlp": "uv tool install yt-dlp  (or pipx install yt-dlp)",
    }
    for tool, hint in hints.items():
        ok = shutil.which(tool)
        print(f"  {'ok ' if ok else 'MISSING'} {tool:<8} {'' if ok else '-> ' + hint}")
    backend_hints = {
        "mlx": "uv tool install mlx-whisper   (Apple Silicon only)",
        "faster-whisper": f"{sys.executable} -m pip install faster-whisper   (must be importable by this python)",
        "whisper": "uv tool install openai-whisper   (slow on CPU)",
    }
    found = [b for b in BACKENDS if backend_available(b)]
    for b in BACKENDS:
        ok = b in found
        print(f"  {'ok ' if ok else 'absent '} backend {b:<15} {'' if ok else '-> ' + backend_hints[b]}")
    if found:
        print(f"auto backend: {pick_backend('auto')}")
    else:
        rec = "mlx" if is_apple_silicon() else "faster-whisper"
        print(f"no backend installed; recommended here: {rec}  ({backend_hints[rec]})")


def cmd_run(a):
    need("ffmpeg"); need("ffprobe")
    vid = video_id(a.source)
    wd = HOME / vid
    wd.mkdir(parents=True, exist_ok=True)
    print(f"[1/4] fetch -> {wd}")
    video, info = fetch(a.source, wd, a.cookies_from_browser)
    meta = probe(video)
    print(f"[2/4] audio (has_audio={meta['has_audio']}, {meta['duration']:.1f}s)")
    segs, lang, backend, model, suspect = [], None, None, None, False
    if meta["has_audio"] and not a.no_audio:
        wav = extract_audio(video, wd)
        backend = pick_backend(a.backend)
        model = pick_model(backend, a.model)
        print(f"[3/4] transcribe with {backend}:{model} (first run downloads the model)")
        segs, lang = TRANSCRIBERS[backend](wav, wd, model, a.lang)
        suspect = looks_hallucinated(segs, meta["duration"])
        write_transcript(segs, lang, wd)
        if suspect:
            print("      warning: transcript looks hallucinated (music or silence?); treat as unreliable")
    else:
        print("[3/4] transcribe skipped (no audio track or --no-audio)")
    print("[4/4] keyframes")
    frames, sheets = keyframes(video, wd, meta["duration"], a.scene, a.max_frames)
    manifest = {
        "id": vid, "source": a.source, "dir": str(wd), "video": video.name,
        "info": info, "probe": meta, "language": lang,
        "transcription": {"backend": backend, "model": model, "suspect": suspect},
        "segments": len(segs), "frames": frames, "sheets": sheets,
        "files": {"srt": "transcript.srt", "txt": "transcript.txt", "segments": "segments.json"},
    }
    (wd / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    print(json.dumps({"id": vid, "dir": str(wd), "duration": meta["duration"], "language": lang,
                      "backend": backend, "transcript_suspect": suspect,
                      "segments": len(segs), "frames": len(frames), "sheets": sheets,
                      "title": info.get("title")}, ensure_ascii=False, indent=1))


def load(vid):
    wd = HOME / vid
    mf = wd / "manifest.json"
    if not mf.exists():
        sys.exit(f"unknown id {vid}; run `list`")
    return wd, json.loads(mf.read_text())


def cmd_frame(a):
    wd, m = load(a.id)
    video = wd / m["video"]
    t0 = parse_ts(a.time)
    out = []
    zdir = wd / "zoom"
    zdir.mkdir(exist_ok=True)
    for i in range(a.n):
        off = (i - (a.n - 1) / 2) * (a.span / max(a.n - 1, 1)) if a.n > 1 else 0
        t = max(0.0, t0 + off)
        f = zdir / f"z_{t:08.2f}.jpg"
        sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
            "-vf", f"scale={a.width}:-2", "-q:v", "2", str(f)])
        out.append(str(f))
    print("\n".join(out))


def cmd_slice(a):
    wd, m = load(a.id)
    backend = pick_backend(a.backend or (m.get("transcription") or {}).get("backend"))
    model = pick_model(backend, a.model)
    s, e = parse_ts(a.start), parse_ts(a.end)
    clip = wd / f"slice_{int(s)}_{int(e)}.wav"
    sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{s:.2f}", "-to", f"{e:.2f}", "-i", str(wd / "audio.wav"),
        "-ac", "1", "-ar", "16000", str(clip)])
    segs, _ = TRANSCRIBERS[backend](clip, wd, model, a.lang)
    segs = [dict(x, start=round(x["start"] + s, 2), end=round(x["end"] + s, 2)) for x in segs]
    write_transcript(segs, None, wd, stem=clip.stem)
    for x in segs:
        print(f"[{fmt_ts(x['start'])}] {x['text']}")


def cmd_list(_):
    if not HOME.exists():
        print("(none)")
        return
    for mf in sorted(HOME.glob("*/manifest.json")):
        m = json.loads(mf.read_text())
        t = (m.get("info") or {}).get("title") or m["source"]
        print(f"{m['id']}\t{m['probe']['duration']:.0f}s\t{t}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    d = sp.add_parser("doctor")
    d.set_defaults(fn=cmd_doctor)
    r = sp.add_parser("run")
    r.add_argument("source")
    r.add_argument("--cookies-from-browser", default=None, help="e.g. chrome (only with user approval)")
    r.add_argument("--lang", default=None)
    r.add_argument("--backend", default=None, help="auto | " + " | ".join(BACKENDS))
    r.add_argument("--model", default=None)
    r.add_argument("--scene", type=float, default=0.3)
    r.add_argument("--max-frames", type=int, default=40)
    r.add_argument("--no-audio", action="store_true")
    r.set_defaults(fn=cmd_run)
    f = sp.add_parser("frame")
    f.add_argument("id"); f.add_argument("time")
    f.add_argument("--n", type=int, default=3)
    f.add_argument("--span", type=float, default=2.0)
    f.add_argument("--width", type=int, default=1280)
    f.set_defaults(fn=cmd_frame)
    s = sp.add_parser("slice")
    s.add_argument("id"); s.add_argument("start"); s.add_argument("end")
    s.add_argument("--lang", default=None)
    s.add_argument("--backend", default=None)
    s.add_argument("--model", default=None)
    s.set_defaults(fn=cmd_slice)
    l = sp.add_parser("list")
    l.set_defaults(fn=cmd_list)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
