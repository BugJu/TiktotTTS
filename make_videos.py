#!/usr/bin/env python3
"""
make_videos.py

Zwei Schritte:
  1. Ein Gameplay-Video in Schnipsel (Standard: 50 Sekunden) zerlegen und in
     einem eigenen Ordner speichern.
  2. Jeden TTS-MP3-Part des Ordners mit einem ZUFAELLIGEN Gameplay-Schnipsel
     zusammenfuegen -> pro Part ein MP4 (Gameplay als Video, TTS als Ton).

Beispiel:
    python make_videos.py test_gameplay.mp4 "My family was terrorized by a demon"

Ergebnis:
    test_gameplay_clips_50s/            <- die 50-s-Gameplay-Schnipsel
    My family was terrorized by a demon/
        video/
            My family was terrorized by a demon pt1.mp4
            My family was terrorized by a demon pt2.mp4
            ...

Braucht ffmpeg. Ist keins installiert, bringt imageio-ffmpeg eins mit:
    python -m pip install --user imageio-ffmpeg
"""

from __future__ import annotations

import argparse
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from glob import glob

try:
    import imageio_ffmpeg

    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    FFMPEG_ERROR: str | None = None
except Exception as _exc:  # noqa: BLE001
    FFMPEG = None
    FFMPEG_ERROR = str(_exc)

FFMPEG_HINT = "python -m pip install --user imageio-ffmpeg"


# ---------------------------------------------------------------------------
# ffmpeg-Helfer
# ---------------------------------------------------------------------------
def ffmpeg_path() -> str:
    if FFMPEG is None:
        raise RuntimeError(f"ffmpeg nicht verfuegbar ({FFMPEG_ERROR}). Installation: {FFMPEG_HINT}")
    return FFMPEG


def run_ffmpeg(args: list[str], verbose: bool = False) -> None:
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", *args]
    if verbose:
        print("  $ ffmpeg " + " ".join(args), file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg-Fehler:\n" + (proc.stderr or "unbekannt")[-800:])


def natural_key(path: str):
    name = os.path.basename(path).lower()
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


# ---------------------------------------------------------------------------
# Schritt 1: Video in Schnipsel zerlegen
# ---------------------------------------------------------------------------
def split_video(video: str, clips_dir: str, segment: int, accurate: bool,
                verbose: bool) -> list[str]:
    if os.path.isdir(clips_dir):
        shutil.rmtree(clips_dir)                 # alte Reste vermeiden
    os.makedirs(clips_dir, exist_ok=True)

    pattern = os.path.join(clips_dir, "clip_%03d.mp4")
    args = ["-y", "-i", video]
    if accurate:
        # Exakte Schnittlaenge, dafuer Re-Encoding.
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                 "-pix_fmt", "yuv420p", "-an"]
    else:
        # Schnell: kopieren, Schnitt an Keyframes (approx. segment-Groesse).
        args += ["-c:v", "copy", "-an"]
    args += ["-f", "segment", "-segment_time", str(segment),
             "-reset_timestamps", "1", pattern]
    run_ffmpeg(args, verbose)

    clips = sorted(glob(os.path.join(clips_dir, "clip_*.mp4")), key=natural_key)
    return clips


# ---------------------------------------------------------------------------
# Schritt 2: Gameplay-Schnipsel + TTS-Audio -> MP4
# ---------------------------------------------------------------------------
def probe_size(path: str) -> tuple[int, int]:
    """(Breite, Hoehe) des ersten Videostreams."""
    out = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", path],
                         capture_output=True, text=True)
    match = re.search(r"Video:.*?,\s*(\d+)x(\d+)", out.stderr)
    return (int(match.group(1)), int(match.group(2))) if match else (1080, 1920)


def _ass_time(seconds: float) -> str:
    centi = max(0, int(round(seconds * 100)))
    hours, rem = divmod(centi, 360000)
    minutes, rem = divmod(rem, 6000)
    secs, cs = divmod(rem, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _parse_srt(text: str) -> list[tuple[float, float, str]]:
    cues: list[tuple[float, float, str]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if len(lines) < 3:
            continue
        match = re.match(
            r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", lines[1].strip()
        )
        if not match:
            continue
        g = list(map(int, match.groups()))
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        cues.append((start, end, "\n".join(lines[2:]).strip()))
    return cues


def _wrap(text: str, max_chars: int) -> list[str]:
    """Bricht den Text an Wortgrenzen auf max_chars Zeichen pro Zeile um."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [text]


def srt_to_ass(srt_path: str, width: int, height: int, font: str = "Arial") -> str:
    """Baut eine ASS-Datei: Text mittig, ohne Hintergrundbox, umbruchsicher."""
    cues = _parse_srt(open(srt_path, encoding="utf-8").read())

    fontsize = max(24, round(height * 0.040))
    outline = max(3, round(height * 0.0030))   # kraeftige Kontur -> lesbar ohne Box
    shadow = max(1, round(height * 0.0016))
    max_width = round(width * 0.86)            # nie breiter als der Frame
    char_w = fontsize * 0.56                   # grobe Zeichenbreite zum Umbruch
    max_chars = max(8, int(max_width / char_w))
    center_x, center_y = width // 2, height // 2

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{fontsize},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},{shadow},5,"
        f"{round(width*0.07)},{round(width*0.07)},0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    events: list[str] = []
    for start, end, text in cues:
        body = "\\N".join(_wrap(text, max_chars))
        events.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,"
            f"{{\\an5\\pos({center_x},{center_y})}}{body}"
        )

    return header + "\n".join(events) + "\n"


def _escape_filter_path(path: str) -> str:
    """Pfad fuer ffmpeg-Filter Windows-sicher aufbereiten (Doppelpunkt escapen)."""
    forward = os.path.abspath(path).replace("\\", "/").replace(":", "\\:")
    return f"'{forward}'"


def combine(clip: str, audio: str, out_path: str, keep_gameplay_audio: bool,
            crf: int, preset: str, verbose: bool,
            subtitle_path: str | None = None, size: tuple[int, int] | None = None) -> None:
    args = [
        "-y",
        "-stream_loop", "-1", "-i", clip,     # Gameplay-Schleife (falls kuerzer als Audio)
        "-i", audio,                          # TTS-Ton
        "-map", "0:v:0",
    ]
    if keep_gameplay_audio:
        args += ["-map", "0:a:0?", "-map", "1:a:0"]
    else:
        args += ["-map", "1:a:0"]             # nur TTS-Ton, Gameplay-Ton verwerfen

    temp_ass: str | None = None
    if subtitle_path and os.path.isfile(subtitle_path):
        width, height = size or probe_size(clip)
        with tempfile.NamedTemporaryFile("w", suffix=".ass", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(srt_to_ass(subtitle_path, width, height))
            temp_ass = handle.name
        args += ["-vf", f"ass={_escape_filter_path(temp_ass)}"]

    args += [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-shortest",                          # Laufzeit = TTS-Laenge
        out_path,
    ]
    try:
        run_ffmpeg(args, verbose)
    finally:
        if temp_ass and os.path.exists(temp_ass):
            os.remove(temp_ass)


def build_videos_for_parts(
    video: str,
    parts: list[str],
    clips_dir: str,
    videos_dir: str,
    *,
    segment: int = 50,
    accurate: bool = False,
    keep_gameplay_audio: bool = False,
    captions: bool = True,
    crf: int = 23,
    preset: str = "veryfast",
    seed: int | None = None,
    reuse_clips: bool = False,
    verbose: bool = False,
    log=print,
) -> tuple[int, int]:
    """Zerlegt `video` (falls noetig) und mischt jeden Part in `parts` mit
    einem zufaelligen Gameplay-Schnipsel zu einem MP4 in `videos_dir`.

    Rueckgabe: (erzeugt, fehlgeschlagen)
    """
    os.makedirs(videos_dir, exist_ok=True)

    if reuse_clips and os.path.isdir(clips_dir) and glob(os.path.join(clips_dir, "*.mp4")):
        clips = sorted(glob(os.path.join(clips_dir, "*.mp4")), key=natural_key)
        log(f"  {len(clips)} Gameplay-Schnipsel wiederverwendet ({clips_dir})")
    else:
        clips = split_video(video, clips_dir, segment, accurate, verbose)
        log(f"  {len(clips)} Gameplay-Schnipsel erzeugt ({clips_dir})")

    if not clips:
        raise RuntimeError("Es wurden keine Gameplay-Schnipsel erzeugt.")

    size = probe_size(clips[0])
    rng = random.Random(seed)
    created = failed = 0
    total = len(parts)
    for index, part in enumerate(parts, start=1):
        base = os.path.splitext(os.path.basename(part))[0]
        out_path = os.path.join(videos_dir, base + ".mp4")
        clip = rng.choice(clips)
        srt = os.path.splitext(part)[0] + ".srt"
        subtitle = srt if (captions and os.path.isfile(srt)) else None
        log(f"  [{index}/{total}] {base}  <-  {os.path.basename(clip)}"
            f"{'  [+Untertitel]' if subtitle else ''}")
        try:
            combine(clip, part, out_path, keep_gameplay_audio, crf, preset, verbose,
                    subtitle_path=subtitle, size=size)
            created += 1
        except Exception as exc:  # noqa: BLE001
            log(f"      FEHLER: {exc}")
            failed += 1

    return created, failed


# ---------------------------------------------------------------------------
# TTS-Ordner finden
# ---------------------------------------------------------------------------
def find_tts_dir() -> str | None:
    candidates = [
        d for d in os.listdir(".")
        if os.path.isdir(d) and glob(os.path.join(d, "*.mp3"))
    ]
    return candidates[0] if len(candidates) == 1 else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if FFMPEG is None:
        print(f"Fehler: ffmpeg fehlt.\nInstallation: {FFMPEG_HINT}", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(
        description="Gameplay in 50-s-Schnipsel teilen und mit TTS-MP3s zu Videos mischen."
    )
    parser.add_argument("video", help="Gameplay-Video (z. B. gameplay.mp4)")
    parser.add_argument("tts_dir", nargs="?",
                        help="Ordner mit den TTS-MP3-Parts (sonst automatisch erkannt)")
    parser.add_argument("--segment", type=int, default=50,
                        help="Laenge der Gameplay-Schnipsel in Sekunden (Standard: 50)")
    parser.add_argument("--clips-dir", help="Zielordner der Gameplay-Schnipsel")
    parser.add_argument("--videos-dir", help="Zielordner der fertigen MP4s")
    parser.add_argument("--skip-split", action="store_true",
                        help="Vorhandene Schnipsel wiederverwenden, nicht neu schneiden")
    parser.add_argument("--accurate", action="store_true",
                        help="Exakte Schnittlaenge (langsamer, Re-Encoding)")
    parser.add_argument("--crf", type=int, default=23, help="Video-Qualitaet (kleiner = besser)")
    parser.add_argument("--preset", default="veryfast", help="x264-Preset (Standard: veryfast)")
    parser.add_argument("--keep-gameplay-audio", action="store_true",
                        help="Original-Gameplay-Ton behalten (statt nur TTS)")
    parser.add_argument("--no-captions", action="store_true",
                        help="Untertitel (.srt) NICHT ins Video einbrennen")
    parser.add_argument("--seed", type=int, help="Zufalls-Seed (reproduzierbare Zuordnung)")
    parser.add_argument("--verbose", action="store_true", help="ffmpeg-Befehle anzeigen")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.video):
        print(f"Fehler: Video nicht gefunden: {args.video}", file=sys.stderr)
        return 1

    # TTS-Ordner bestimmen
    tts_dir = args.tts_dir
    if tts_dir is None:
        tts_dir = find_tts_dir()
        if tts_dir is None:
            print("Fehler: Kein TTS-Ordner angegeben und keiner eindeutig erkannt.",
                  file=sys.stderr)
            return 1
        print(f"(TTS-Ordner automatisch erkannt: {tts_dir})")
    if not os.path.isdir(tts_dir):
        print(f"Fehler: TTS-Ordner nicht gefunden: {tts_dir}", file=sys.stderr)
        return 1

    parts = sorted(glob(os.path.join(tts_dir, "*.mp3")), key=natural_key)
    if not parts:
        print(f"Fehler: Keine MP3-Dateien in {tts_dir}", file=sys.stderr)
        return 1

    # Schritt 1: Gameplay zerlegen
    stem = os.path.splitext(os.path.basename(args.video))[0]
    clips_dir = args.clips_dir or f"{stem}_clips_{args.segment}s"

    if args.skip_split and os.path.isdir(clips_dir) and glob(os.path.join(clips_dir, "*.mp4")):
        clips = sorted(glob(os.path.join(clips_dir, "*.mp4")), key=natural_key)
        print(f"Schritt 1: {len(clips)} vorhandene Schnipsel wiederverwendet ({clips_dir})")
    else:
        print(f"Schritt 1: Video in {args.segment}-s-Schnipsel zerlegen ...")
        clips = split_video(args.video, clips_dir, args.segment, args.accurate, args.verbose)
        print(f"           -> {len(clips)} Schnipsel in: {clips_dir}")

    if not clips:
        print("Fehler: Es wurden keine Schnipsel erzeugt.", file=sys.stderr)
        return 1

    size = probe_size(clips[0])

    # Schritt 2: pro TTS-Part ein Random-Schnipsel dranhaengen
    videos_dir = args.videos_dir or os.path.join(tts_dir, "video")
    os.makedirs(videos_dir, exist_ok=True)

    rng = random.Random(args.seed)
    print(f"Schritt 2: {len(parts)} Videos erzeugen in: {videos_dir}")
    print("=" * 60)

    ok = failed = 0
    for index, part in enumerate(parts, start=1):
        base = os.path.splitext(os.path.basename(part))[0]
        out_path = os.path.join(videos_dir, base + ".mp4")
        clip = rng.choice(clips)
        srt = os.path.splitext(part)[0] + ".srt"
        subtitle = srt if (not args.no_captions and os.path.isfile(srt)) else None
        print(f"[{index}/{len(parts)}] {base}")
        print(f"    Gameplay: {os.path.basename(clip)}"
              f"{'  [+Untertitel]' if subtitle else ''}")
        try:
            combine(clip, part, out_path, args.keep_gameplay_audio,
                    args.crf, args.preset, args.verbose, subtitle_path=subtitle, size=size)
            print(f"    -> {os.path.basename(out_path)} ({os.path.getsize(out_path):,} Bytes)")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"    FEHLER: {exc}", file=sys.stderr)
            failed += 1

    print("=" * 60)
    print(f"Fertig: {ok} Videos erzeugt, {failed} fehlgeschlagen.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
