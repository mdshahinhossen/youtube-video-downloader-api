import glob
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
    allowed = {
        "youtube.com", "www.youtube.com", "m.youtube.com",
        "youtu.be", "www.youtu.be", "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
    if parsed.scheme not in ("http", "https") or host not in allowed:
        raise ValueError("Only valid YouTube URLs are supported.")
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/").split("/")[0]
    elif parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    else:
        parts = parsed.path.strip("/").split("/")
        video_id = parts[1] if len(parts) >= 2 and parts[0] in {
            "shorts", "embed", "live", "v"
        } else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Invalid YouTube video URL.")
    return url


def ydl_options(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 25,
        "retries": 2,
    }
    try:
        import imageio_ffmpeg
        opts["ffmpeg_location"] = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    if extra:
        opts.update(extra)
    return opts


def extract_info(url):
    with yt_dlp.YoutubeDL(ydl_options({"skip_download": True})) as ydl:
        return ydl.extract_info(url, download=False)


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
    if not os.path.isfile(os.path.join(BASE_DIR, "tester.html")):
        return jsonify({"success": False, "error": "tester.html was not found."}), 404
    return send_from_directory(BASE_DIR, "tester.html")


@app.post("/video_info")
def video_info():
    try:
        data = request.get_json(silent=True) or {}
        info = extract_info(validate_url(data.get("url")))
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
        app.logger.exception("Video information request failed")
        return jsonify({"success": False, "error": str(exc)}), 502


@app.post("/available_resolutions")
def available_resolutions():
    try:
        data = request.get_json(silent=True) or {}
        info = extract_info(validate_url(data.get("url")))
        heights = set()
        for fmt in info.get("formats", []):
            if fmt.get("height") and fmt.get("vcodec") not in (None, "none"):
                if fmt.get("ext") == "mp4":
                    heights.add(int(fmt["height"]))
        resolutions = [f"{height}p" for height in sorted(heights)]
        if not resolutions:
            return jsonify({
                "success": False,
                "resolutions": [],
                "error": "YouTube returned no MP4 video resolutions for this video.",
            }), 502
        return jsonify({"success": True, "resolutions": resolutions})
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("Resolution request failed")
        return jsonify({"success": False, "error": str(exc)}), 502


@app.post("/download/<resolution>")
def download_video(resolution):
    if not re.fullmatch(r"\d{3,4}p", resolution):
        return jsonify({"success": False, "error": "Invalid resolution."}), 400

    temp_dir = None
    try:
        data = request.get_json(silent=True) or {}
        url = validate_url(data.get("url"))
        height = int(resolution[:-1])
        temp_dir = tempfile.mkdtemp(prefix="yt_download_")
        output_template = os.path.join(temp_dir, "video.%(ext)s")
        format_selector = (
            f"bestvideo[height={height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[height={height}][ext=mp4]/best[height={height}]"
        )
        options = ydl_options({
            "format": format_selector,
            "outtmpl": output_template,
            "merge_output_format": "mp4",
        })
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title") or "youtube_video"

        files = [
            path for path in glob.glob(os.path.join(temp_dir, "video.*"))
            if os.path.isfile(path) and not path.endswith((".part", ".ytdl"))
        ]
        if not files:
            raise RuntimeError("No output video file was created.")
        file_path = max(files, key=os.path.getsize)
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip()[:100] or "youtube_video"
        response = send_file(
            file_path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"{safe_title}_{resolution}.mp4",
        )
        response.call_on_close(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return response
    except ValueError as exc:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        app.logger.exception("Video download failed")
        return jsonify({"success": False, "error": str(exc)}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=False)
