from flask import Flask, request, jsonify, send_file
import subprocess
import os
import uuid
import threading
import time
import re
import json

app = Flask(__name__)
AUDIO_DIR = "/tmp/audio"
SILENT_MP3 = os.path.join(AUDIO_DIR, "silence.mp3")
os.makedirs(AUDIO_DIR, exist_ok=True)


def _create_silent_mp3():
    if os.path.exists(SILENT_MP3):
        return
    try:
        subprocess.run(
            ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
             "-t", "1", "-q:a", "9", SILENT_MP3],
            capture_output=True, timeout=10)
    except Exception:
        with open(SILENT_MP3, "wb") as f:
            f.write(b'\xff\xfb\x90\x00' + b'\x00' * 417)

_create_silent_mp3()


def cleanup_old_files():
    while True:
        now = time.time()
        for f in os.listdir(AUDIO_DIR):
            if f == "silence.mp3":
                continue
            path = os.path.join(AUDIO_DIR, f)
            try:
                if now - os.path.getmtime(path) > 600:
                    os.remove(path)
            except Exception:
                pass
        time.sleep(60)

threading.Thread(target=cleanup_old_files, daemon=True).start()


# ── Smart matching ──────────────────────────────────────────────

def _normalize(text):
    """Lowercase, strip punctuation, collapse spaces."""
    t = text.lower()
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _score_match(query, title):
    """
    Score how well a YouTube title matches the user's query.
    Higher = better match. Range roughly 0-100.
    """
    q = _normalize(query)
    t = _normalize(title)
    q_words = set(q.split())
    t_words = set(t.split())

    if not q_words:
        return 0

    # What fraction of query words appear in the title?
    matched = q_words & t_words
    word_overlap = len(matched) / len(q_words)

    # Bonus: does the title contain the query as a substring?
    substring_bonus = 0.2 if q in t else 0

    # Bonus: exact match of key phrases (artist - song patterns)
    # Split query on common separators
    q_parts = [p.strip() for p in re.split(r'\bby\b|\b-\b', q) if p.strip()]
    parts_bonus = 0
    for part in q_parts:
        if part in t:
            parts_bonus += 0.15

    # Penalty for very long titles (likely compilations, mixes, playlists)
    length_penalty = 0
    if len(t_words) > 15:
        length_penalty = 0.15
    elif len(t_words) > 10:
        length_penalty = 0.05

    # Penalty for "full album", "playlist", "mix", "compilation"
    noise_words = {"full album", "playlist", "mix", "compilation", "medley", "collection", "hour", "hours"}
    noise_penalty = 0
    for nw in noise_words:
        if nw in t:
            noise_penalty = 0.2
            break

    score = (word_overlap * 0.6) + substring_bonus + parts_bonus - length_penalty - noise_penalty
    return max(0, min(1, score)) * 100


def _search_and_pick_best(query, num_results=5):
    """
    Search YouTube for multiple results, return the URL of the best title match.
    Falls back to first result if scoring fails.
    """
    try:
        # Get titles and URLs without downloading
        result = subprocess.run(
            [
                "yt-dlp",
                "--no-playlist",
                "--js-runtimes", "node",
                "--print", "%(title)s\t%(webpage_url)s",
                "--no-download",
                "ytsearch%d:%s" % (num_results, query),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0 or not result.stdout.strip():
            print("Search failed for '%s': %s" % (query, (result.stderr or "")[-200:]))
            return None

        # Parse results: each line is "title\turl"
        candidates = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if "\t" not in line:
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2:
                title, url = parts
                candidates.append((title.strip(), url.strip()))

        if not candidates:
            print("No candidates found for '%s'" % query)
            return None

        # Score each candidate
        best_url = candidates[0][1]  # fallback to first
        best_score = -1
        for title, url in candidates:
            score = _score_match(query, title)
            print("  Score %.1f: '%s' -> %s" % (score, title[:60], url[:50]))
            if score > best_score:
                best_score = score
                best_url = url

        print("Best match for '%s': score=%.1f url=%s" % (query, best_score, best_url[:60]))
        return best_url

    except subprocess.TimeoutExpired:
        print("Search timeout for '%s'" % query)
        return None
    except Exception as e:
        print("Search error for '%s': %s" % (query, e))
        return None


def _download_url(url, output_template):
    """Download a specific YouTube URL as MP3."""
    return subprocess.run(
        [
            "yt-dlp",
            "-f", "worstaudio",
            "--no-playlist",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "10",
            "--js-runtimes", "node",
            "-o", output_template,
            url,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _run_ytdlp_simple(query, output_template):
    """Fallback: single search+download in one step (less accurate but faster)."""
    return subprocess.run(
        [
            "yt-dlp",
            "-f", "worstaudio",
            "--no-playlist",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "10",
            "--js-runtimes", "node",
            "-o", output_template,
            "ytsearch1:%s" % query,
        ],
        capture_output=True,
        text=True,
        timeout=40,
    )


# ── Routes ──────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "Song server is running!"})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/prepare", methods=["GET"])
def prepare_audio():
    """Smart download: search 5 results, pick best title match, download it."""
    query = request.args.get("q", "")
    if not query:
        return jsonify({"ok": False})

    file_id = str(uuid.uuid4())[:8]
    output_template = os.path.join(AUDIO_DIR, "%s.%%(ext)s" % file_id)

    try:
        # Step 1: Search and find best matching URL
        best_url = _search_and_pick_best(query)

        if best_url:
            # Step 2: Download the best match
            result = _download_url(best_url, output_template)
        else:
            # Fallback: simple ytsearch1 if search failed
            print("Falling back to simple search for '%s'" % query)
            result = _run_ytdlp_simple(query, output_template)

        # Find the downloaded file
        for f in os.listdir(AUDIO_DIR):
            if f.startswith(file_id) and f.endswith(".mp3"):
                base_url = "https://%s" % request.host
                play_url = "%s/files/%s" % (base_url, f)
                print("Prepare OK: '%s' -> %s" % (query, play_url))
                return jsonify({"ok": True, "url": play_url})

        print("Prepare FAIL: '%s' stderr: %s" % (query, (result.stderr or "")[-200:]))
    except subprocess.TimeoutExpired:
        print("Prepare timeout: '%s'" % query)
    except Exception as e:
        print("Prepare error: %s" % e)

    return jsonify({"ok": False})


@app.route("/stream", methods=["GET"])
def stream_audio():
    """ALWAYS returns audio — smart match or silent fallback."""
    query = request.args.get("q", "")
    if not query:
        return send_file(SILENT_MP3, mimetype="audio/mpeg")

    file_id = str(uuid.uuid4())[:8]
    output_template = os.path.join(AUDIO_DIR, "%s.%%(ext)s" % file_id)

    try:
        # Try smart match first
        best_url = _search_and_pick_best(query)

        if best_url:
            _download_url(best_url, output_template)
        else:
            _run_ytdlp_simple(query, output_template)

        for f in os.listdir(AUDIO_DIR):
            if f.startswith(file_id) and f.endswith(".mp3"):
                actual_file = os.path.join(AUDIO_DIR, f)
                return send_file(actual_file, mimetype="audio/mpeg")

        print("Stream failed for '%s'" % query)
    except subprocess.TimeoutExpired:
        print("Stream timeout for '%s'" % query)
    except Exception as e:
        print("Stream error for '%s': %s" % (query, e))

    return send_file(SILENT_MP3, mimetype="audio/mpeg")


@app.route("/files/<filename>", methods=["GET"])
def serve_file(filename):
    if "/" in filename or ".." in filename:
        return send_file(SILENT_MP3, mimetype="audio/mpeg")
    filepath = os.path.join(AUDIO_DIR, filename)
    if not os.path.exists(filepath):
        return send_file(SILENT_MP3, mimetype="audio/mpeg")
    return send_file(filepath, mimetype="audio/mpeg")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
