from flask import Flask, request, jsonify, send_file
import subprocess
import os
import uuid
import threading
import time

app = Flask(__name__)
AUDIO_DIR = "/tmp/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)


def cleanup_old_files():
    """Delete audio files older than 30 minutes"""
    while True:
        now = time.time()
        for f in os.listdir(AUDIO_DIR):
            path = os.path.join(AUDIO_DIR, f)
            try:
                if now - os.path.getmtime(path) > 1800:
                    os.remove(path)
            except Exception:
                pass
        time.sleep(60)


threading.Thread(target=cleanup_old_files, daemon=True).start()


def make_safe_name(query):
    """Turn a search query into a safe filename"""
    safe = "".join(c if c.isalnum() or c in " -" else "" for c in query)
    safe = safe[:50].strip().replace(" ", "_")
    return safe


def download_and_compress(query, safe_name, file_id, output_template, max_seconds="540"):
    """Step 1: Download full audio. Step 2: Compress with ffmpeg."""

    # Step 1: Download full audio (no compression, no trimming)
    result = subprocess.run(
        [
            "yt-dlp",
            "-f", "bestaudio",
            "--no-playlist",
            "-x",
            "--audio-format", "mp3",
            "--match-filter", "duration<600",
            "--no-warnings",
            "--no-check-certificates",
            "--extractor-args", "youtube:player_client=mediaconnect",
            "-o", output_template,
            f"ytsearch1:{query}",
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )

    # Find the downloaded file
    raw_file = None
    for f in os.listdir(AUDIO_DIR):
        if f.startswith(safe_name) and f.endswith(".mp3"):
            raw_file = os.path.join(AUDIO_DIR, f)
            break

    if not raw_file or not os.path.exists(raw_file):
        return None, result.stderr[-500:] if result.stderr else "no error output"

    # Step 2: Compress with ffmpeg separately
    compressed_file = raw_file.replace(".mp3", "_small.mp3")
    compress_result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", raw_file,
            "-t", max_seconds,
            "-b:a", "8k",
            "-ac", "1",
            "-ar", "8000",
            compressed_file,
        ],
        capture_output=True,
        timeout=30,
    )

    # Remove the large original and rename compressed
    if os.path.exists(compressed_file):
        os.remove(raw_file)
        os.rename(compressed_file, raw_file)
        return raw_file, None
    else:
        # Compression failed, return original file as-is
        return raw_file, None


@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "MP3 retrieval server is running!"})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/download", methods=["GET"])
def download_audio():
    """Downloads audio from YouTube, converts to MP3, and returns a direct URL"""
    query = request.args.get("q", "")
    if not query:
        return jsonify({"error": "No query provided. Use ?q=song+name"}), 400

    max_seconds = request.args.get("sec", "540")

    file_id = str(uuid.uuid4())[:8]
    safe_name = make_safe_name(query)
    filename_base = f"{safe_name}_{file_id}"
    output_template = os.path.join(AUDIO_DIR, f"{filename_base}.%(ext)s")

    try:
        filepath, error = download_and_compress(query, safe_name, file_id, output_template, max_seconds)

        if not filepath:
            return jsonify({
                "error": "Could not find or convert audio",
                "stderr": error,
            }), 500

        filename = os.path.basename(filepath)
        base_url = request.host_url.rstrip("/")
        file_url = f"{base_url}/files/{filename}"
        file_size = os.path.getsize(filepath)

        return jsonify({
            "url": file_url,
            "filename": filename,
            "size_bytes": file_size,
            "query": query,
            "expires_in": "30 minutes",
        })

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Download timed out"}), 504


@app.route("/stream", methods=["GET"])
def stream_audio():
    """Downloads audio and serves it directly as an MP3 (no redirect)"""
    query = request.args.get("q", "")
    if not query:
        return jsonify({"error": "No query provided. Use ?q=song+name"}), 400

    max_seconds = request.args.get("sec", "540")

    file_id = str(uuid.uuid4())[:8]
    safe_name = make_safe_name(query)
    filename_base = f"{safe_name}_{file_id}"
    output_template = os.path.join(AUDIO_DIR, f"{filename_base}.%(ext)s")

    try:
        filepath, error = download_and_compress(query, safe_name, file_id, output_template, max_seconds)

        if not filepath:
            return jsonify({
                "error": "Could not find or convert audio",
                "stderr": error,
            }), 500

        return send_file(filepath, mimetype="audio/mpeg")

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Download timed out"}), 504


@app.route("/files/<filename>", methods=["GET"])
def serve_file(filename):
    """Serves a stored MP3 file directly by filename"""
    if "/" in filename or ".." in filename:
        return jsonify({"error": "Invalid filename"}), 400

    filepath = os.path.join(AUDIO_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found or expired"}), 404

    return send_file(filepath, mimetype="audio/mpeg")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
