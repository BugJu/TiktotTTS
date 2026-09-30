# Reddit Story → TikTok-Video (TTS + Untertitel)

Wandelt einen **Reddit-Post** automatisch in fertige **Hochkant-Videos** um:
Story vertont (Microsoft Edge-TTS) + Gameplay im Hintergrund + **wortgenau
synchronisierte Untertitel** direkt ins Video eingebrannt + **Part-Titel**
oben eingeblendet.

Ein einziger Befehl reicht:

```powershell
python reddit_chunker.py "<reddit-link>"
```

## Pipeline

```
Reddit-Link
   │  reddit_chunker.py  (Holen + Aufteilen)
   ▼
<Titel>/<Titel> pt1..ptN.txt      Story-Schnipsel (Standard: 700 Zeichen)
   │  tts_chunks.py      (Vertonen, edge-tts)
   ▼
<Titel>/<Titel> pt1..ptN.mp3      Sprache
   + <Titel> pt1..ptN.srt         Wort-genaue Untertitel
   │  make_videos.py     (Gameplay + ffmpeg)
   ▼
<Titel>/video/<Titel> pt1..ptN.mp4   fertige Videos
```

- **Schnipsel** werden sauber an Wortgrenzen geschnitten (max. 700 Zeichen).
- **Untertitel** kommen aus den `WordBoundary`-Events von edge-tts und sind
  daher exakt an die Sprache gekoppelt.
- **Gameplay** wird in Abschnitte (`--segment`, Standard 50 s) zerlegt und pro
  Schnipsel zufällig gewählt, bis die Audio-Länge erreicht ist.

## Voraussetzungen

- Python 3.10+
- Pakete: `edge-tts`, `requests`
- `ffmpeg` (wird über `imageio-ffmpeg` automatisch mitgeliefert)

## Installation

```powershell
python -m pip install --user edge-tts requests imageio-ffmpeg
```

## Schnellstart

```powershell
# 1. Gameplay-Video nach gameplays/ legen (z. B. minecraft.mp4)

# 2. Komplette Pipeline
python reddit_chunker.py "https://www.reddit.com/r/..."
```

## Optionen

### `reddit_chunker.py` (Gesamt-Pipeline)

| Option | Bedeutung |
|---|---|
| `--size N` | Zeichen pro Schnipsel (Standard 700) |
| `--comments` | Top-Kommentare mit vertonen |
| `--voice ID` | TTS-Stimme (Standard: en-US-AndrewNeural) |
| `--rate/--volume/--pitch` | z. B. `+10%`, `+5%`, `+2Hz` |
| `--outdir PFAD` | Basisordner (Standard `.`) |
| `--overwrite` | Vorhandene MP3s neu erzeugen |
| `--no-tts` | nur Schnipsel, kein Audio |
| `--no-captions` | keine Untertitel erzeugen/einbrennen |
| `--no-title` | Part-Titel (`<Titel> PT. <n>`) nicht oben einblenden |
| `--no-video` | nur Audio, kein Video |
| `--gameplay PFAD` | Gameplay-Datei/-Ordner erzwingen |
| `--segment N` | Gameplay-Schnipsel-Länge in Sekunden |
| `--seed N` | reproduzierbare Schnipsel-Zuordnung |
| `--keep-gameplay-audio` | Originalton behalten (sonst nur TTS) |
| `--crf N` / `--preset X` | Videoqualität / x264-Preset |
| `--clips-dir/-videos-dir` | Zielordner |
| `--show` / `--verbose` | Diagnose / ffmpeg-Befehle anzeigen |

### `tts_chunks.py` (nur vertonen)

```powershell
python tts_chunks.py "<ordner>"        # erzeugt .mp3 + .srt
```
Optionen u. a. `--voice`, `--rate`, `--pattern`, `--ext`, `--no-captions`,
`--list-voices`, `--retries`.

### `make_videos.py` (nur Video)

```powershell
python make_videos.py "<gameplay>" "<ordner>"
```
Optionen u. a. `--skip-split` (vorhandene Schnipsel wiederverwenden),
`--accurate`, `--no-captions`, `--keep-gameplay-audio`, `--seed` u. a.

## Untertitel

- **Position:** mittig im Video
- **Stil:** weisser Text mit kräftiger schwarzer Kontur (kein Hintergrundkasten)
- **Umbruch:** automatisch an der Video-Breite (max. 86 %), kein Überlauf
- Quelle: `WordBoundary`-Timings von edge-tts → wortgenau

Deaktivieren: `--no-captions`.

## Part-Titel (Kopf-Einblendung)

Zusätzlich zu den gesprochenen Untertiteln wird oben im Video dauerhaft der
Part-Titel angezeigt, z. B.:

```
I GOT TRAPPED IN THE MALL PT. 1
```

- **Schreibweise:** Der Titel wird in **Original-Schreibweise** ausgegeben –
  es wird nichts groß- oder kleingeschrieben.
- **Aufbau:** `<Titel> PT. <n>` (Titel gefolgt von Part-Nummer).
- **Stil:** weisser Text mit kräftiger schwarzer Kontur (**kein** Hintergrundkasten),
  deutlich kleiner als die Untertitel und auf **maximal 2 Zeilen** begrenzt
  (balanciert umgebrochen, Schrift wird bei Bedarf verkleinert).
- **Position:** oben mittig.
- Wird der Titel nicht explizit übergeben (z. B. bei `make_videos.py`), wird
  er aus dem Dateinamen abgeleitet (`<Titel> pt<n>` → `<Titel> PT. <n>`).

Deaktivieren: `--no-title`.

## Hinweise

- Große Medien (`gameplays/*.mp4`) und erzeugte Ausgaben (`<Titel>/…`,
  `*_clips_50s/`) sind per `.gitignore` ausgeschlossen.
- Der eingeblendete Text enthält keine Satzzeichen, da die
  `WordBoundary`-Events von edge-tts die Interpunktion nicht mitliefern.

## Lizenz

Privates Projekt – keine Lizenz angegeben.
