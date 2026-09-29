#!/usr/bin/env python3
"""
tts_chunks.py

Wandelt die .txt-Schnipsel eines Ordners in Sprache um - pro Schnipsel
eine Audio-Datei (MP3), standardmaessig im GLEICHEN Ordner.

Damit wird aus:
    My family was terrorized by a demon/
        My family was terrorized by a demon pt1.txt
        My family was terrorized by a demon pt2.txt
        ...

das hier:
    My family was terrorized by a demon/
        My family was terrorized by a demon pt1.txt
        My family was terrorized by a demon pt1.mp3
        My family was terrorized by a demon pt2.mp3
        ...

Verwendet edge-tts (Microsoft Neural-Stimmen, kostenlos, kein ffmpeg noetig).
Installation:  python -m pip install --user edge-tts

Beispiele:
    python tts_chunks.py "My family was terrorized by a demon"
    python tts_chunks.py "My family was terrorized by a demon" --voice en-US-GuyNeural
    python tts_chunks.py "My family ..." --rate +10% --pitch -2Hz
    python tts_chunks.py "My family ..." --outdir "My family ... (TTS)"
    python tts_chunks.py --list-voices en-US
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from glob import glob

try:
    import edge_tts
    EDGE_TTS_ERROR: str | None = None
except ImportError as _exc:  # noqa: BLE001 - Paket optional, Fehler spaeter melden
    edge_tts = None
    EDGE_TTS_ERROR = str(_exc)

DEFAULT_VOICE = "en-US-AndrewNeural"
INSTALL_HINT = "python -m pip install --user edge-tts"


def ensure_available() -> None:
    """Wirft RuntimeError, wenn edge-tts nicht installiert ist."""
    if edge_tts is None:
        raise RuntimeError(f"Paket 'edge-tts' fehlt. Installation: {INSTALL_HINT}")


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------
def natural_key(path: str):
    """Sortierschluessel, damit pt2 vor pt10 kommt (keine reine Text-Sortierung)."""
    name = os.path.basename(path).lower()
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def collect_text_files(folder: str, pattern: str) -> list[str]:
    files = glob(os.path.join(folder, pattern))
    files = [f for f in files if os.path.isfile(f)]
    return sorted(files, key=natural_key)


async def list_voices(filter_text: str = "") -> None:
    ensure_available()
    voices = await edge_tts.list_voices()
    needle = filter_text.lower()
    for voice in sorted(voices, key=lambda v: v["ShortName"]):
        short = voice["ShortName"]
        if needle and needle not in short.lower() and needle not in voice.get("Locale", "").lower():
            continue
        gender = voice.get("Gender", "?")
        print(f"{short:40} {gender:7} {voice.get('FriendlyName', '')}")


async def synthesize_with_timings(
    text: str, out_path: str, voice: str, rate: str, volume: str,
    pitch: str, retries: int,
) -> list[tuple[float, float, str]]:
    """Spricht den Text, speichert das Audio und liefert die Wort-Zeiten.

    Rueckgabe: Liste von (start_sekunden, end_sekunden, wort)
    """
    ensure_available()
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            words: list[tuple[float, float, str]] = []
            communicate = edge_tts.Communicate(
                text, voice, rate=rate, volume=volume, pitch=pitch,
                boundary="WordBoundary",
            )
            with open(out_path, "wb") as handle:
                async for event in communicate.stream():
                    if event["type"] == "audio":
                        handle.write(event["data"])
                    elif event["type"] == "WordBoundary":
                        # offset/duration in 100-Nanosekunden-Ticks
                        start = event["offset"] / 1e7
                        words.append((start, start + event["duration"] / 1e7, event["text"]))
            return words
        except Exception as exc:  # noqa: BLE001 - Netzwerk-/Dienstfehler
            last_error = exc
            if attempt < retries:
                await asyncio.sleep(2 * attempt)
    raise RuntimeError(f"Synthese fehlgeschlagen: {last_error}")


async def synthesize(
    text: str, out_path: str, voice: str, rate: str, volume: str, pitch: str, retries: int
) -> None:
    await synthesize_with_timings(text, out_path, voice, rate, volume, pitch, retries)


def _srt_time(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_cues(
    words: list[tuple[float, float, str]], max_chars: int = 32, max_words: int = 5
) -> list[tuple[float, float, str]]:
    """Gruppiert die Worte zu kurzen Untertitel-Zeilen (die gerade gesprochenen)."""
    cues: list[tuple[float, float, str]] = []
    current: list[tuple[float, float, str]] = []

    def flush() -> None:
        nonlocal current
        if current:
            cues.append((current[0][0], current[-1][1], " ".join(w[2] for w in current)))
            current = []

    for word in words:
        current.append(word)
        joined = " ".join(w[2] for w in current)
        ends_sentence = word[2].strip().endswith((".", "!", "?", ":", ";", "\u2026"))
        if len(joined) >= max_chars or len(current) >= max_words or ends_sentence:
            flush()
    flush()

    # Luecken schliessen: eine Zeile bleibt stehen, bis die naechste erscheint.
    for index in range(len(cues) - 1):
        cues[index] = (cues[index][0], max(cues[index][1], cues[index + 1][0]), cues[index][2])
    return cues


def write_srt(cues: list[tuple[float, float, str]], path: str) -> None:
    blocks = [
        f"{index}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n"
        for index, (start, end, text) in enumerate(cues, start=1)
    ]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(blocks))


def write_captions(
    words: list[tuple[float, float, str]], srt_path: str,
    max_chars: int = 32, max_words: int = 5,
) -> bool:
    """Baut aus den Wort-Zeiten eine SRT-Datei. False, wenn keine Worte vorliegen."""
    if not words:
        return False
    write_srt(build_cues(words, max_chars, max_words), srt_path)
    return True


# ---------------------------------------------------------------------------
# Ablauf
# ---------------------------------------------------------------------------
async def run(args) -> int:
    folder = args.folder
    if not os.path.isdir(folder):
        print(f"Fehler: Ordner nicht gefunden: {folder}", file=sys.stderr)
        return 1

    text_files = collect_text_files(folder, args.pattern)
    if not text_files:
        print(f"Fehler: Keine Dateien nach Muster '{args.pattern}' in {folder}", file=sys.stderr)
        return 1

    out_dir = args.outdir or folder
    os.makedirs(out_dir, exist_ok=True)

    print(f"Ordner  : {folder}")
    print(f"Ausgabe : {out_dir}")
    print(f"Stimme  : {args.voice}  (rate={args.rate}, volume={args.volume}, pitch={args.pitch})")
    print(f"Dateien : {len(text_files)}")
    print("=" * 60)

    ok = skipped = failed = 0
    for index, path in enumerate(text_files, start=1):
        base = os.path.splitext(os.path.basename(path))[0]
        out_path = os.path.join(out_dir, base + args.ext)

        if os.path.exists(out_path) and not args.overwrite:
            print(f"[{index}/{len(text_files)}] uebersprungen (existiert): {base}{args.ext}")
            skipped += 1
            continue

        text = open(path, encoding="utf-8").read().strip()
        if not text:
            print(f"[{index}/{len(text_files)}] leer, uebersprungen: {base}")
            skipped += 1
            continue

        print(f"[{index}/{len(text_files)}] spreche: {base} ...")
        try:
            words = await synthesize_with_timings(
                text, out_path, args.voice, args.rate, args.volume, args.pitch, args.retries
            )
            if not args.no_captions:
                write_captions(words, os.path.splitext(out_path)[0] + ".srt")
            size = os.path.getsize(out_path)
            print(f"            -> {os.path.basename(out_path)} ({size:,} Bytes)")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"            FEHLER: {exc}", file=sys.stderr)
            failed += 1

    print("=" * 60)
    print(f"Erledigt: {ok} erzeugt, {skipped} uebersprungen, {failed} fehlgeschlagen.")
    return 1 if failed else 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if edge_tts is None:
        print(f"Fehler: Paket 'edge-tts' fehlt.\nInstallation: {INSTALL_HINT}", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(
        description="Die .txt-Schnipsel eines Ordners in Audio (MP3) umwandeln."
    )
    parser.add_argument("folder", nargs="?", help="Ordner mit den .txt-Schnipseln")
    parser.add_argument("--voice", default=DEFAULT_VOICE,
                        help=f"Stimme (Standard: {DEFAULT_VOICE})")
    parser.add_argument("--rate", default="+0%", help="Sprechgeschwindigkeit, z. B. +10%% / -5%%")
    parser.add_argument("--volume", default="+0%", help="Lautstaerke, z. B. +10%%")
    parser.add_argument("--pitch", default="+0Hz", help="Tonhoehe, z. B. +2Hz / -2Hz")
    parser.add_argument("--pattern", default="*.txt", help="Datei-Muster (Standard: *.txt)")
    parser.add_argument("--ext", default=".mp3", help="Endung der Audio-Dateien (Standard: .mp3)")
    parser.add_argument("--outdir", help="Zielordner (Standard: derselbe Ordner)")
    parser.add_argument("--overwrite", action="store_true",
                        help="Vorhandene Audio-Dateien neu erzeugen")
    parser.add_argument("--no-captions", action="store_true",
                        help="Keine .srt-Untertitel erzeugen")
    parser.add_argument("--retries", type=int, default=3, help="Versuche je Schnipsel bei Fehlern")
    parser.add_argument("--list-voices", nargs="?", const="", metavar="FILTER",
                        help="Verfuegbare Stimmen auflisten (optional nach Sprache filtern)",
                        )
    args = parser.parse_args(argv)

    if args.list_voices is not None:
        asyncio.run(list_voices(args.list_voices))
        return 0

    if not args.folder:
        parser.error("Bitte einen Ordner angeben (oder --list-voices nutzen).")

    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
