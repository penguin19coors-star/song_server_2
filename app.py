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
    """Delete audio files older than 60 minutes"""
    while True:
        now = time.time()
        for f in os.listdir(AUDIO_DIR):
            path = os.path.join(AUDIO_DIR, f)
            try:
                if now - os.path.getmtime(path) > 3600:
                    os.remove(path)
            except Exception:
                pass
        time.sleep(60)


threading.Thread(target=cleanup_old_files, daemon=True).start()


def make_safe_name(query):
    """Turn a search query into a safe filename"""
    safe = "".join(c if c.isalnum() or c in " -" else "" for c in query)
    safe = safe[:50].strip().replace(" ", "_")
    if not safe:
        safe = "audio"
    return safe


def run_ytdlp(query, output_template):
    """Run yt-dlp with exact same settings as the working phone server"""
    return subprocess.run(
        [
            "yt-dlp",
            "-f", "bestaudio[filesize<10M]/bestaudio",
            "--no-playlist",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "10",
            "-o", output_template,
            f"ytsearch1:{query}",
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )


def find_mp3(prefix):
    """Find an MP3 file in AUDIO_DIR starting with the given prefix"""
    for f in os.listdir(AUDIO_DIR):
        if f.startswith(prefix) and f.endswith(".mp3"):
            return f
    return None


QUALITY_PRESETS = {
    "low": {"bitrate": "8k", "sample_rate": "8000", "channels": "1"},
    "medium": {"bitrate": "64k", "sample_rate": "22050", "channels": "1"},
    "high": {"bitrate": "128k", "sample_rate": "44100", "channels": "2"},
    "max": {"bitrate": "192k", "sample_rate": "44100", "channels": "2"},
}


@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "MP3 retrieval server is running!"})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/stream", methods=["GET"])
def stream_audio():
    """Downloads YouTube audio, converts to MP3, and serves it directly"""
    query = request.args.get("q", "")
    if not query:
        return jsonify({"error": "No query provided. Use ?q=song+name"}), 400

    file_id = str(uuid.uuid4())[:8]
    output_template = os.path.join(AUDIO_DIR, f"{file_id}.%(ext)s")

    try:
        result = run_ytdlp(query, output_template)

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


@app.route("/download", methods=["GET"])
def download_audio():
    """Downloads audio, optionally re-encodes, returns a direct URL"""
    query = request.args.get("q", "")
    if not query:
        return jsonify({"error": "No query provided. Use ?q=song+name"}), 400

    quality = request.args.get("quality", "low")
    if quality not in QUALITY_PRESETS:
        return jsonify({"error": f"Invalid quality. Options: {list(QUALITY_PRESETS.keys())}"}), 400

    max_seconds = request.args.get("sec", "540")

    file_id = str(uuid.uuid4())[:8]
    safe_name = make_safe_name(query)
    filename_base = f"{safe_name}_{file_id}"
    output_template = os.path.join(AUDIO_DIR, f"{filename_base}.%(ext)s")

    try:
        # Step 1: Download with exact same settings as phone server
        result = run_ytdlp(query, output_template)

        raw_filename = find_mp3(safe_name)
        if not raw_filename:
            return jsonify({
                "error": "Could not find or convert audio",
                "stderr": result.stderr[-500:] if result.stderr else "no error output",
            }), 500

        raw_file = os.path.join(AUDIO_DIR, raw_filename)

        # Step 2: Re-encode at chosen quality with ffmpeg
        preset = QUALITY_PRESETS[quality]
        compressed_file = raw_file.replace(".mp3", "_dl.mp3")
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", raw_file,
                "-t", max_seconds,
                "-b:a", preset["bitrate"],
                "-ac", preset["channels"],
                "-ar", preset["sample_rate"],
                compressed_file,
            ],
            capture_output=True,
            timeout=60,
        )

        if os.path.exists(compressed_file):
            os.remove(raw_file)
            os.rename(compressed_file, raw_file)

        filename = os.path.basename(raw_file)
        base_url = f"https://{request.host}"
        file_url = f"{base_url}/files/{filename}"
        file_size = os.path.getsize(raw_file)
        file_size_mb = round(file_size / (1024 * 1024), 2)

        return jsonify({
            "url": file_url,
            "filename": filename,
            "size_bytes": file_size,
            "size_mb": file_size_mb,
            "quality": quality,
            "bitrate": preset["bitrate"],
            "query": query,
            "expires_in": "60 minutes",
        })

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
