import os
import re
import shutil
import tempfile
from urllib.parse import urlparse, parse_qs

from flask import Flask, request, jsonify, send_file, send_from_directory
import yt_dlp

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def validate_url(url):
    if not isinstance(url, str) or not url.strip():
        raise ValueError("YouTube URL is required.")

    url = url.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed_hosts = {
        "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
        "youtu.be", "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
    }

    if parsed.scheme not in ("http", "https") or host not in allowed_hosts:
        raise ValueError("Only valid YouTube URLs are supported.")

    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/").split("/")[0]
    elif parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    else:
        parts = parsed.path.strip("/").split("/")
        video_id = parts[1] if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"} else ""

    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Invalid YouTube video URL.")

    return url


def ydl_options(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        # Keep retries modest; repeated retries can worsen rate limiting.
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
    }
    try:
        import imageio_ffmpeg
        opts["ffmpeg_location"] = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        # Railway may provide ffmpeg through its system PATH instead.
        pass
    if extra:
        opts.update(extra)
    return opts


def extract_info(url):
    options = ydl_options({"skip_download": True})
    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


def error_response(exc, default_message):
    message = str(exc)
    lowered = message.lower()
    if "429" in message or "too many requests" in lowered:
        return jsonify({
            "success": False,
            "error": "YouTube is temporarily rate-limiting requests. Please wait and try again later."
        }), 429
    if "private video" in lowered or "video is unavailable" in lowered or "unavailable" in lowered:
        return jsonify({"success": False, "error": "This video is unavailable or cannot be accessed."}), 404
    app.logger.exception(default_message)
    return jsonify({"success": False, "error": default_message, "details": message}), 502


@app.get("/")
def home():
    return jsonify({
        "success": True,
        "name": "YouTube Video Downloader API",
        "status": "running",
        "mode": "automatic_best_quality",
        "endpoints": [
            "GET /", "GET /health", "GET /tester",
            "POST /video_info", "POST /download",
        ],
        "usage": {"method": "POST", "endpoint": "/download", "json": {"url": "https://www.youtube.com/watch?v=VIDEO_ID"}},
    })


@app.get("/health")
def health():
    return jsonify({"success": True, "status": "healthy"})


@app.get("/tester")
def tester():
    file_path = os.path.join(BASE_DIR, "tester.html")
    if not os.path.isfile(file_path):
        return jsonify({"success": False, "error": "tester.html was not found."}), 404
    return send_from_directory(BASE_DIR, "tester.html")


@app.post("/video_info")
def video_info():
    try:
        data = request.get_json(silent=True) or {}
        url = validate_url(data.get("url"))
        info = extract_info(url)
        return jsonify({
            "success": True,
            "title": info.get("title"),
            "author": info.get("channel") or info.get("uploader"),
            "length": info.get("duration"),
            "views": info.get("view_count"),
            "description": info.get("description"),
            "publish_date": info.get("upload_date"),
            "thumbnail_url": info.get("thumbnail"),
        })
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        return error_response(exc, "Could not retrieve video information.")


@app.post("/download")
def download_video():
    temp_dir = None
    try:
        data = request.get_json(silent=True) or {}
        url = validate_url(data.get("url"))
        temp_dir = tempfile.mkdtemp(prefix="yt_download_")
        output_template = os.path.join(temp_dir, "video.%(ext)s")

        # Prefer best MP4 video + M4A audio; fall back to a combined MP4
        # or any best available video/audio pair that yt-dlp can merge.
        format_selector = (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "best[ext=mp4]/"
            "bestvideo+bestaudio/best"
        )
        options = ydl_options({
            "format": format_selector,
            "outtmpl": output_template,
            "merge_output_format": "mp4",
            "overwrites": True,
        })

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title") or "youtube_video"

        excluded_suffixes = (".part", ".ytdl", ".json", ".description", ".jpg", ".webp", ".png")
        files = [
            path for path in os.listdir(temp_dir)
            if os.path.isfile(os.path.join(temp_dir, path)) and not path.endswith(excluded_suffixes)
        ]
        if not files:
            raise RuntimeError("Download finished but no video file was created. FFmpeg may be missing or the selected format may be unavailable.")

        file_path = os.path.join(temp_dir, max(files, key=lambda name: os.path.getsize(os.path.join(temp_dir, name))))
        safe_title = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", title).strip()[:100] or "youtube_video"
        extension = os.path.splitext(file_path)[1].lower()
        mimetype = "video/mp4" if extension == ".mp4" else "application/octet-stream"

        response = send_file(
            file_path,
            mimetype=mimetype,
            as_attachment=True,
            download_name=f"{safe_title}{extension or '.mp4'}",
            conditional=False,
        )
        response.call_on_close(lambda path=temp_dir: shutil.rmtree(path, ignore_errors=True))
        return response

    except ValueError as exc:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return error_response(exc, "Video download failed. Please try again later.")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), debug=False)
