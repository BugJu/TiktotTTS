#!/usr/bin/env python3


"""
reddit_chunker.py  -  komplette Reddit-zu-Video-Pipeline

Holt den Text eines Reddit-Posts (optional inkl. Kommentare), teilt ihn in
Schnipsel (Standard: 700 Zeichen, sauber an Wortgrenzen) und erzeugt:

    <Titel>/<Titel> pt1.txt ... ptN.txt        (Schnipsel)
    <Titel>/<Titel> pt1.mp3 ... ptN.mp3        (Sprache, edge-tts)
    <Titel>/<Titel> pt1.srt ... ptN.srt        (Untertitel, wortgenau)
    <Titel>/video/<Titel> pt1.mp4 ... ptN.mp4  (fertige Hochkant-Videos)

Ein einziger Befehl reicht:
    python reddit_chunker.py "<reddit-link>"

Abruf-Strategie (automatisch, von oben nach unten):
    1. Reddit-JSON (.json)  -> sauber strukturierte Daten
    2. Reddit-RSS  (.rss)   -> Fallback, den Reddit seltener blockt
Bei Rate-Limits (HTTP 429) wird automatisch gewartet und erneut versucht.

Der TTS-Teil braucht das Paket edge-tts:  python -m pip install --user edge-tts

Beispiele:
    python reddit_chunker.py <URL>                      # Ordner + Schnipsel + MP3
    python reddit_chunker.py <URL> --size 500           # andere Schnipsel-Groesse
    python reddit_chunker.py <URL> --comments           # Kommentare mit vertonen
    python reddit_chunker.py <URL> --voice en-US-BrianNeural
    python reddit_chunker.py <URL> --no-tts             # nur Schnipsel, kein Audio
    python reddit_chunker.py <URL> --outdir C:\\audio    # Basisordner
    python reddit_chunker.py <URL> --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from glob import glob
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

import make_videos as mv
import tts_chunks as tts

# Reddit mag es, wenn ein echter Client-User-Agent gesetzt ist.
# Ueberschreibbar per Umgebungsvariable:  REDDIT_UA="..." python reddit_chunker.py ...
USER_AGENT = os.environ.get(
    "REDDIT_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
)
HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}

TIMEOUT = 25
MAX_ATTEMPTS = 4          # Versuche pro URL bei 429
RETRY_DELAY = 6           # Basisdelay in Sekunden (waechst je Versuch)

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


class FetchError(Exception):
    """Wird geworfen, wenn der Reddit-Inhalt nicht geladen werden kann."""


# ---------------------------------------------------------------------------
# URL-Handling
# ---------------------------------------------------------------------------
def _ensure_scheme(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _host_variants(host: str) -> list[str]:
    """Reddit antwortet je Host unterschiedlich; daher mehrere probieren."""
    order = [host, "www.reddit.com", "old.reddit.com"]
    result: list[str] = []
    for h in order:
        if h and h not in result and "reddit.com" in h:
            result.append(h)
    if not result:
        result = ["www.reddit.com", "old.reddit.com"]
    return result


def build_json_urls(url: str) -> list[str]:
    parts = urlsplit(_ensure_scheme(url))
    path = parts.path.rstrip("/")
    if path.endswith(".rss"):
        path = path[:-4]
    if not path.endswith(".json"):
        path += ".json"
    return [urlunsplit((parts.scheme, h, path, "", "")) for h in _host_variants(parts.netloc)]


def build_rss_urls(url: str) -> list[str]:
    parts = urlsplit(_ensure_scheme(url))
    path = parts.path.rstrip("/")
    if path.endswith(".json"):
        path = path[:-5]
    if not path.endswith(".rss"):
        path += ".rss"
    return [urlunsplit((parts.scheme, h, path, "", "")) for h in _host_variants(parts.netloc)]


# ---------------------------------------------------------------------------
# HTTP mit Retry
# ---------------------------------------------------------------------------
def _http_get(url: str, verbose: bool = False) -> tuple[bytes | None, str | None]:
    """GET mit automatischem Retry bei HTTP 429.  -> (daten, fehlertext)"""
    last_error = "unbekannt"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            request = Request(url, headers=HEADERS)
            with urlopen(request, timeout=TIMEOUT) as response:
                return response.read(), None
        except HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code == 429 and attempt < MAX_ATTEMPTS:
                wait = RETRY_DELAY * attempt
                if verbose:
                    print(f"  429 (zu viele Anfragen) - warte {wait}s ...", file=sys.stderr)
                time.sleep(wait)
                continue
            return None, last_error
        except URLError as exc:
            return None, f"Netzwerkfehler ({exc.reason})"
    return None, last_error


# ---------------------------------------------------------------------------
# Inhalte normalisieren:  {"title", "selftext", "comments":[(autor, text)]}
# ---------------------------------------------------------------------------
def normalize_from_json(data) -> dict:
    post = data[0]["data"]["children"][0]["data"]
    title = (post.get("title") or "").strip()
    selftext = (post.get("selftext") or "").strip()

    comments: list[tuple[str, str]] = []
    if len(data) > 1:
        def walk(children):
            for child in children:
                if child.get("kind") != "t1":
                    continue
                payload = child["data"]
                body = (payload.get("body") or "").strip()
                if body:
                    comments.append((payload.get("author") or "?", body))
                replies = payload.get("replies")
                if isinstance(replies, dict):
                    walk(replies["data"]["children"])

        walk(data[1]["data"]["children"])

    return {"title": title, "selftext": selftext, "comments": comments}


def _decode_xml(data: bytes) -> str:
    match = re.match(rb'<\?xml[^>]*encoding=["\']([^"\']+)', data[:200])
    encoding = match.group(1).decode("ascii", "ignore") if match else "utf-8"
    try:
        return data.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return data.decode("utf-8", "replace")


def normalize_from_rss(data: bytes) -> dict:
    root = ET.fromstring(_decode_xml(data))
    entries = root.findall("a:entry", ATOM_NS)
    if not entries:
        raise ValueError("Feed enthaelt keine Eintraege")

    post_entry = entries[0]
    title = (post_entry.findtext("a:title", default="", namespaces=ATOM_NS) or "").strip()
    content = post_entry.findtext("a:content", default="", namespaces=ATOM_NS) or ""
    selftext = html_to_text(content)
    # Reddits RSS haengt an den Post eine Fusszeile an -> entfernen.
    selftext = re.sub(
        r"\s*submitted by\s*/u/\S+\s*\[link\]\s*\[comments\]\s*$", "", selftext
    ).strip()

    comments: list[tuple[str, str]] = []
    for entry in entries[1:]:
        author = (entry.findtext("a:author/a:name", default="", namespaces=ATOM_NS) or "").strip()
        body = entry.findtext("a:content", default="", namespaces=ATOM_NS) or ""
        text = html_to_text(body)
        if text:
            comments.append((author, text))

    return {"title": title, "selftext": selftext, "comments": comments}


def fetch_content(url: str, verbose: bool = False) -> dict:
    """Liefert die normalisierten Inhalte des Posts (JSON, sonst RSS)."""
    diagnostics: list[str] = []

    # 1) Versuch: JSON-Endpunkt
    for target in build_json_urls(url):
        data, error = _http_get(target, verbose)
        if data is None:
            diagnostics.append(f"JSON {target} -> {error}")
            continue
        stripped = data.lstrip()
        if stripped[:1] not in (b"[", b"{"):
            diagnostics.append(f"JSON {target} -> HTML statt JSON (Bot-Sperre)")
            continue
        try:
            return normalize_from_json(json.loads(data.decode("utf-8", "replace")))
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            diagnostics.append(f"JSON {target} -> unerwartetes Format ({type(exc).__name__})")

    # 2) Versuch: RSS-Fallback
    for target in build_rss_urls(url):
        data, error = _http_get(target, verbose)
        if data is None:
            diagnostics.append(f"RSS {target} -> {error}")
            continue
        try:
            return normalize_from_rss(data)
        except (ET.ParseError, ValueError) as exc:
            diagnostics.append(f"RSS {target} -> unlesbar ({type(exc).__name__})")

    raise FetchError("Reddit-Inhalt konnte nicht geladen werden.\n  " + "\n  ".join(diagnostics))


# ---------------------------------------------------------------------------
# HTML -> Klartext  /  Text saeubern
# ---------------------------------------------------------------------------
def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)      # Leerzeichen vor Zeilenumbruch
    text = re.sub(r"\n[ \t]+", "\n", text)      # Leerzeichen nach Zeilenumbruch
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_text(fragment: str) -> str:
    """Wandelt Reddits HTML-Fragment (aus dem RSS) in lesbaren Klartext um."""
    text = html.unescape(fragment)                     # &lt;p&gt; -> <p>
    text = text.replace("\xa0", " ")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|blockquote|tr)\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)                # restliche Tags weg
    text = html.unescape(text)                         # restliche Entities
    return clean_text(text)


# ---------------------------------------------------------------------------
# In Schnipsel teilen
# ---------------------------------------------------------------------------
def chunk_text(text: str, size: int = 500) -> list[str]:
    """Teilt den Text in Stuecke von max. `size` Zeichen.

    Es wird an Wortgrenzen getrennt, damit kein Wort zerrissen wird.
    Absaetze bleiben erhalten. Ein einzelnes ueberlanges Wort wird notfalls
    hart getrennt.
    """
    text = clean_text(text)
    if not text:
        return []

    # Jeder Token = ein Wort plus der darauf folgende Leerraum.
    tokens = re.findall(r"\S+\s*", text)

    chunks: list[str] = []
    buffer = ""

    for token in tokens:
        if len(token) > size:                      # Sonderfall: Riesen-Wort
            if buffer:
                chunks.append(buffer.rstrip())
                buffer = ""
            for start in range(0, len(token), size):
                piece = token[start:start + size]
                if len(piece) == size:
                    chunks.append(piece)
                else:
                    buffer = piece
            continue

        if len(buffer) + len(token) <= size:
            buffer += token
        else:
            chunks.append(buffer.rstrip())
            buffer = token

    if buffer.strip():
        chunks.append(buffer.rstrip())

    return chunks


# ---------------------------------------------------------------------------
# Aufbau des Gesamttextes aus den normalisierten Daten
# ---------------------------------------------------------------------------
def build_text(content: dict, include_comments: bool) -> str:
    parts: list[str] = []
    if content.get("title"):
        parts.append(content["title"])
    if content.get("selftext"):
        parts.append(content["selftext"])
    if include_comments:
        for author, body in content.get("comments", []):
            parts.append(f"[{author}]: {body}")
    return clean_text("\n\n".join(parts))


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """Macht aus einem Titel einen gueltigen Ordner-/Dateinamen (auch Windows)."""
    name = re.sub(r"[\x00-\x1f]", "", name or "")   # Steuerzeichen weg
    for ch in '<>:"/\\|?*':                          # unter Windows verboten
        name = name.replace(ch, "")
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(". ")                          # kein Punkt/Leerzeichen am Ende
    if not name:
        name = "reddit_post"
    return name[:max_len].rstrip(". ")


def _normalize_ext(ext: str) -> str:
    ext = (ext or "").strip()
    if ext and not ext.startswith("."):
        ext = "." + ext
    return ext or ".txt"


def save_chunks_to_folder(
    chunks: list[str], title: str, base_dir: str = ".", ext: str = ".txt"
) -> tuple[str, list[str]]:
    """Legt einen Ordner mit dem Post-Titel an und schreibt je Schnipsel
    eine Datei  "<Titel> pt<n><ext>"  hinein.

    Rueckgabe: (ordnerpfad, liste_der_dateipfade)
    """
    ext = _normalize_ext(ext)
    name = sanitize_filename(title)
    folder = os.path.join(base_dir, name)
    os.makedirs(folder, exist_ok=True)

    written: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        file_path = os.path.join(folder, f"{name} pt{index}{ext}")
        with open(file_path, "w", encoding="utf-8") as handle:
            handle.write(chunk)
        written.append(file_path)

    return folder, written


# ---------------------------------------------------------------------------
# TTS: je Schnipsel eine MP3-Datei
# ---------------------------------------------------------------------------
async def generate_audio(
    chunks: list[str], folder: str, title: str, voice: str,
    rate: str, volume: str, pitch: str, overwrite: bool, retries: int,
    captions: bool = True,
) -> tuple[int, int, int]:
    """Spricht jeden Schnipsel, speichert ihn als "<Titel> pt<n>.mp3" und
    (optional) die Wort-Untertitel als "<Titel> pt<n>.srt".

    Rueckgabe: (erzeugt, uebersprungen, fehlgeschlagen)
    """
    name = sanitize_filename(title)
    total = len(chunks)
    created = skipped = failed = 0

    for index, chunk in enumerate(chunks, start=1):
        out_path = os.path.join(folder, f"{name} pt{index}.mp3")

        if os.path.exists(out_path) and not overwrite:
            print(f"  [{index}/{total}] uebersprungen (existiert): pt{index}.mp3")
            skipped += 1
            continue

        print(f"  [{index}/{total}] spreche pt{index} ...")
        try:
            words = await tts.synthesize_with_timings(
                chunk, out_path, voice, rate, volume, pitch, retries
            )
            if captions:
                tts.write_captions(words, os.path.splitext(out_path)[0] + ".srt")
            print(f"            -> pt{index}.mp3 ({os.path.getsize(out_path):,} Bytes)")
            created += 1
        except Exception as exc:  # noqa: BLE001
            print(f"            FEHLER bei pt{index}: {exc}", file=sys.stderr)
            failed += 1

    return created, skipped, failed


def find_gameplay(explicit: str | None = None) -> str | None:
    """Findet das Gameplay-Video: explizit, sonst in 'gameplays/', sonst *.mp4 hier."""
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    if os.path.isdir("gameplays"):
        found = sorted(glob(os.path.join("gameplays", "*.mp4")))
        if found:
            return found[0]
    found = [f for f in glob("*.mp4") if os.path.isfile(f)]
    return found[0] if found else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    # Auf Windows-Konsolen ist die Kodierung oft cp1252 -> Sonderzeichen wuerden
    # sonst einen UnicodeEncodeError ausloesen. Darum explizit auf UTF-8 stellen.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Reddit-Post laden, in Schnipsel teilen und vertonen (komplette Pipeline)."
    )
    parser.add_argument("url", help="Reddit-Link zum Post")
    parser.add_argument("--size", type=int, default=700,
                        help="Maximale Zeichen pro Schnipsel (Standard: 700)")
    parser.add_argument("--comments", action="store_true", help="Kommentare mit einbeziehen")
    parser.add_argument("--voice", default=tts.DEFAULT_VOICE,
                        help=f"TTS-Stimme (Standard: {tts.DEFAULT_VOICE})")
    parser.add_argument("--rate", default="+0%", help="Sprechgeschwindigkeit, z. B. +10%% / -5%%")
    parser.add_argument("--volume", default="+0%", help="Lautstaerke, z. B. +10%%")
    parser.add_argument("--pitch", default="+0Hz", help="Tonhoehe, z. B. +2Hz / -2Hz")
    parser.add_argument("--outdir", default=".", help="Basisordner (Standard: .)")
    parser.add_argument("--no-tts", action="store_true", help="Nur Schnipsel, kein Audio")
    parser.add_argument("--overwrite", action="store_true", help="Vorhandene MP3s neu erzeugen")
    parser.add_argument("--no-captions", action="store_true",
                        help="Keine Untertitel (.srt) erzeugen/einbrennen")
    parser.add_argument("--no-title", action="store_true",
                        help="Part-Titel (<Titel> PT. <n>) nicht oben einblenden")

    # Video-Teil
    parser.add_argument("--gameplay",
                        help="Gameplay-Video (sonst: Ordner 'gameplays/' bzw. *.mp4 hier)")
    parser.add_argument("--no-video", action="store_true", help="Nur Audio, kein Video")
    parser.add_argument("--segment", type=int, default=50,
                        help="Laenge der Gameplay-Schnipsel in Sekunden (Standard: 50)")
    parser.add_argument("--seed", type=int, help="Zufalls-Seed fuer die Schnipsel-Zuordnung")
    parser.add_argument("--keep-gameplay-audio", action="store_true",
                        help="Original-Gameplay-Ton behalten (statt nur TTS)")
    parser.add_argument("--crf", type=int, default=23, help="Video-Qualitaet (kleiner = besser)")
    parser.add_argument("--preset", default="veryfast", help="x264-Preset (Standard: veryfast)")
    parser.add_argument("--clips-dir", help="Ordner der Gameplay-Schnipsel")
    parser.add_argument("--videos-dir", help="Zielordner der fertigen Videos")

    parser.add_argument("--show", action="store_true",
                        help="Schnipsel zusaetzlich auf der Konsole ausgeben")
    parser.add_argument("--verbose", action="store_true", help="Diagnose-Ausgaben anzeigen")
    args = parser.parse_args(argv)

    if args.size < 1:
        parser.error("--size muss mindestens 1 sein.")

    # 1) Reddit-Inhalt holen
    try:
        content = fetch_content(args.url, verbose=args.verbose)
    except FetchError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1

    text = build_text(content, include_comments=args.comments)
    if not text.strip():
        print("Dieser Post enthaelt keinen Text (evtl. ein Link- oder Bild-Post).", file=sys.stderr)
        return 2

    title = content.get("title", "")
    chunks = chunk_text(text, args.size)

    print(f"Titel          : {title}")
    print(f"Zeichen gesamt : {len(text)}")
    print(f"Schnipsel      : {len(chunks)} (max. {args.size} Zeichen)")

    if args.show:
        print("=" * 60)
        for index, chunk in enumerate(chunks, start=1):
            print(f"[{index}/{len(chunks)}] ({len(chunk)} Zeichen)")
            print(chunk)
            print("-" * 60)

    # 2) Schnipsel als .txt in den <Titel>-Ordner schreiben
    folder, txt_files = save_chunks_to_folder(chunks, title, args.outdir, ".txt")
    print("=" * 60)
    print(f"Ordner         : {folder}")
    print(f"Text-Schnipsel : {len(txt_files)} .txt-Dateien geschrieben")

    # 3) Vertonen
    audio_failed = 0
    if args.no_tts:
        print("TTS            : uebersprungen (--no-tts)")
    elif tts.edge_tts is None:
        print("TTS            : uebersprungen - Paket 'edge-tts' fehlt.", file=sys.stderr)
        print(f"                 Installation: {tts.INSTALL_HINT}", file=sys.stderr)
    else:
        print(f"TTS            : {args.voice}"
              f" (rate={args.rate}, volume={args.volume}, pitch={args.pitch})")
        created, skipped, audio_failed = asyncio.run(
            generate_audio(chunks, folder, title, args.voice, args.rate,
                           args.volume, args.pitch, args.overwrite, 3,
                           captions=not args.no_captions)
        )
        print(f"               -> {created} MP3 erzeugt, {skipped} uebersprungen,"
              f" {audio_failed} fehlgeschlagen")

    # 4) Video: jeden TTS-Part mit zufaelligem Gameplay-Schnipsel kombinieren
    if args.no_video:
        print("Video          : uebersprungen (--no-video)")
        return 1 if audio_failed else 0

    parts = sorted(glob(os.path.join(folder, "*.mp3")), key=mv.natural_key)
    if not parts:
        print("Video          : uebersprungen - keine MP3-Parts vorhanden.", file=sys.stderr)
        return 1 if audio_failed else 0

    if mv.FFMPEG is None:
        print("Video          : uebersprungen - ffmpeg fehlt.", file=sys.stderr)
        print(f"                 Installation: {mv.FFMPEG_HINT}", file=sys.stderr)
        return 1 if audio_failed else 0

    gameplay = find_gameplay(args.gameplay)
    if not gameplay:
        print("Video          : uebersprungen - kein Gameplay gefunden.", file=sys.stderr)
        print("                 Lege ein Video in 'gameplays/' oder nutze --gameplay PFAD.",
              file=sys.stderr)
        return 1 if audio_failed else 0

    clips_dir = args.clips_dir or f"gameplay_clips_{args.segment}s"
    videos_dir = args.videos_dir or os.path.join(folder, "video")

    # Kopf-Einblendung: "<Reddit-Titel> PT. <n>" in Originalschreibweise.
    titles = {} if args.no_title else mv.title_map_for_parts(parts, title)

    print("=" * 60)
    print(f"Video          : {os.path.basename(gameplay)}  ({len(parts)} Parts)")
    print(f"Schnipsel      : {clips_dir} ({args.segment}s)")
    print(f"Videos         : {videos_dir}")
    print(f"Part-Titel     : {'aus (--no-title)' if args.no_title else title + ' PT. <n>'}")
    try:
        v_ok, v_failed = mv.build_videos_for_parts(
            gameplay, parts, clips_dir, videos_dir,
            segment=args.segment, keep_gameplay_audio=args.keep_gameplay_audio,
            captions=not args.no_captions, show_title=not args.no_title, titles=titles,
            crf=args.crf, preset=args.preset,
            seed=args.seed, reuse_clips=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Fehler beim Video-Erstellen: {exc}", file=sys.stderr)
        return 1

    print("=" * 60)
    print(f"Fertig: {v_ok} Videos erzeugt, {v_failed} fehlgeschlagen.")
    return 1 if (v_failed or audio_failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
