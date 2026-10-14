import glob
import os
import re
import shutil
import tempfile
from urllib.parse import parse_qs, urlparse

import yt_dlp
from flask import Flask, jsonify, request, send_file, send_from_directory

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
}


def validate_url(url):
    """Validate common YouTube watch, Shorts, live, embed, and youtu.be URLs."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("YouTube URL is required.")

    url = url.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if parsed.scheme not in ("http", "https") or host not in YOUTUBE_HOSTS:
        raise ValueError("Only valid YouTube URLs are supported.")

    video_id = ""
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/").split("/")[0]
    elif parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    else:
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
            video_id = parts[1]

    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id or ""):
        raise ValueError("Invalid YouTube video URL.")

    return url


def ydl_options(extra=None):
    """Options shared by metadata extraction and downloads.

    Avoid forcing skip_download here because the download endpoint must actually
    download the media. Retries are deliberately limited: repeated requests do
    not bypass YouTube's HTTP 429 rate limits.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 1,
        "extractor_retries": 1,
        "fragment_retries": 2,
    }

    try:
        import imageio_ffmpeg
        opts["ffmpeg_location"] = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        # yt-dlp can still work for progressive streams without a bundled FFmpeg.
        pass

    if extra:
        opts.update(extra)
    return opts


def extract_info(url):
    with yt_dlp.YoutubeDL(ydl_options({"skip_download": True})) as ydl:
        return ydl.extract_info(url, download=False)


def is_rate_limit_error(exc):
    message = str(exc).lower()
    return "http error 429" in message or "too many requests" in message or "429" in message


def error_response(exc, operation):
    app.logger.exception("%s failed", operation)
    if is_rate_limit_error(exc):
        return jsonify({
            "success": False,
            "error": "YouTube is rate-limiting requests (HTTP 429). Wait before retrying; repeated requests may extend the limit.",
        }), 429
    return jsonify({
        "success": False,
        "error": str(exc) or f"{operation} failed.",
    }), 502


@app.get("/")
def home():
    return jsonify({
        "success": True,
        "name": "YouTube Video Downloader API",
        "status": "running",
        "endpoints": [
            "GET /", "GET /health", "GET /tester",
            "POST /video_info", "POST /available_resolutions",
            "POST /download/<resolution>",
        ],
    })


@app.get("/health")
def health():
    return jsonify({"success": True, "status": "healthy"})


@app.get("/tester")
def tester():
    path = os.path.join(BASE_DIR, "tester.html")
    if not os.path.isfile(path):
        return jsonify({"success": False, "error": "tester.html was not found."}), 404
    return send_from_directory(BASE_DIR, "tester.html")


@app.post("/video_info")
def video_info():
    data = request.get_json(silent=True) or {}
    try:
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
        return error_response(exc, "Video information request")


@app.post("/available_resolutions")
def available_resolutions():
    data = request.get_json(silent=True) or {}
    try:
        url = validate_url(data.get("url"))
        info = extract_info(url)
        formats = info.get("formats") or []
        mp4_heights = set()
        other_heights = set()

        for fmt in formats:
            height = fmt.get("height")
            vcodec = fmt.get("vcodec")
            if not height or not vcodec or vcodec == "none":
                continue
            try:
                height = int(height)
            except (TypeError, ValueError):
                continue
            if fmt.get("ext") == "mp4":
                mp4_heights.add(height)
            other_heights.add(height)

        # Prefer MP4 formats; if none exist, return heights from other video formats.
        heights = mp4_heights or other_heights
        if not heights:
            return jsonify({
                "success": False,
                "resolutions": [],
                "error": "No video formats were returned. The video may be unavailable or YouTube may be rate-limiting requests.",
            }), 502

        return jsonify({
            "success": True,
            "title": info.get("title"),
            "resolutions": [f"{height}p" for height in sorted(heights)],
        })
    except ValueError as exc:
        return jsonify({"success": False, "resolutions": [], "error": str(exc)}), 400
    except Exception as exc:
        response, status = error_response(exc, "Resolution extraction")
        # Preserve a stable response shape for the tester UI.
        payload = response.get_json()
        payload["resolutions"] = []
        return jsonify(payload), status


@app.post("/download/<resolution>")
def download_video(resolution):
    if not re.fullmatch(r"\d{3,4}p", resolution):
        return jsonify({"success": False, "error": "Invalid resolution. Use values such as 360p or 720p."}), 400

    temp_dir = None
    try:
        data = request.get_json(silent=True) or {}
        url = validate_url(data.get("url"))
        height = int(resolution[:-1])
        temp_dir = tempfile.mkdtemp(prefix="yt_download_")
        output_template = os.path.join(temp_dir, "video.%(ext)s")

        # Prefer MP4 video + M4A audio, then a combined MP4 stream, then any
        # available format at the requested height. FFmpeg merges separate streams.
        format_selector = (
            f"bestvideo[height={height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height={height}]+bestaudio/"
            f"best[height={height}][ext=mp4]/best[height={height}]"
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

        files = [
            path for path in glob.glob(os.path.join(temp_dir, "video.*"))
            if os.path.isfile(path) and not path.endswith((".part", ".ytdl", ".json"))
        ]
        if not files:
            raise RuntimeError("No output video file was created. Check whether FFmpeg is available and the requested resolution exists.")

        file_path = max(files, key=os.path.getsize)
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip()[:100] or "youtube_video"
        response = send_file(
            file_path,
            mimetype="video/mp4" if file_path.endswith(".mp4") else "application/octet-stream",
            as_attachment=True,
            download_name=f"{safe_title}_{resolution}{os.path.splitext(file_path)[1] or '.mp4'}",
        )
        # The temporary file is removed after Flask finishes serving the response.
        response.call_on_close(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return response

    except ValueError as exc:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return error_response(exc, "Video download")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=False)
