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

    file_id = str(uuid.uuid4())[:8]
    output_template = os.path.join(AUDIO_DIR, f"{file_id}.%(ext)s")

    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "-f", "bestaudio[filesize<15M]/bestaudio",
                "--no-playlist",
                "-x",
                "--audio-format", "mp3",
                "--postprocessor-args", "ffmpeg:-b:a 8k -ac 1 -ar 22050",
                "--download-sections", "*00:00:00-00:03:00",
                "--match-filter", "duration<600",
                "--no-warnings",
                "--no-check-certificates",
                "-o", output_template,
                f"ytsearch1:{query}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

        actual_file = None
        for f in os.listdir(AUDIO_DIR):
            if f.startswith(file_id) and f.endswith(".mp3"):
                actual_file = f
                break

        if not actual_file:
            return jsonify({
                "error": "Could not find or convert audio",
                "stderr": result.stderr[-500:] if result.stderr else "no error output",
            }), 500

        base_url = request.host_url.rstrip("/")
        file_url = f"{base_url}/files/{actual_file}"
        file_path = os.path.join(AUDIO_DIR, actual_file)
        file_size = os.path.getsize(file_path)

        return jsonify({
            "url": file_url,
            "filename": actual_file,
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

    file_id = str(uuid.uuid4())[:8]
    output_template = os.path.join(AUDIO_DIR, f"{file_id}.%(ext)s")

    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "-f", "bestaudio[filesize<15M]/bestaudio",
                "--no-playlist",
                "-x",
                "--audio-format", "mp3",
                "--postprocessor-args", "ffmpeg:-b:a 8k -ac 1 -ar 22050",
                "--download-sections", "*00:00:00-00:03:00",
                "--match-filter", "duration<600",
                "--no-warnings",
                "--no-check-certificates",
                "-o", output_template,
                f"ytsearch1:{query}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

        actual_file = None
        for f in os.listdir(AUDIO_DIR):
            if f.startswith(file_id) and f.endswith(".mp3"):
                actual_file = os.path.join(AUDIO_DIR, f)
                break

        if not actual_file or not os.path.exists(actual_file):
            return jsonify({
                "error": "Could not find or convert audio",
                "stderr": result.stderr[-500:] if result.stderr else "no error output",
            }), 500

        return send_file(actual_file, mimetype="audio/mpeg")

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
