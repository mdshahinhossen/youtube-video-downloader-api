import os
import re
import tempfile
from urllib.parse import urlparse, parse_qs

from flask import (
    Flask,
    request,
    jsonify,
    send_file,
    send_from_directory,
    after_this_request
)
from pytubefix import YouTube

app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def get_youtube(url):
    """Validate a YouTube URL and create a YouTube object."""

    if not isinstance(url, str) or not url.strip():
        raise ValueError("YouTube URL is required.")

    url = url.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    allowed_hosts = {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
        "www.youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }

    if host not in allowed_hosts:
        raise ValueError("Only YouTube URLs are supported.")

    if parsed.scheme not in ("http", "https"):
        raise ValueError("Invalid URL scheme.")

    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/").split("/")[0]

    elif parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]

    else:
        parts = parsed.path.strip("/").split("/")

        if len(parts) >= 2 and parts[0] in {
            "shorts",
            "embed",
            "live",
            "v",
        }:
            video_id = parts[1]
        else:
            raise ValueError("Invalid YouTube video URL.")

    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Invalid YouTube video URL.")

    return YouTube(url)


@app.get("/")
def home():
    return jsonify({
        "success": True,
        "name": "YouTube Video Downloader API",
        "status": "running",
        "endpoints": [
            "GET /",
            "GET /health",
            "GET /tester",
            "POST /video_info",
            "POST /available_resolutions",
            "POST /download/<resolution>"
        ]
    })


@app.get("/health")
def health():
    return jsonify({
        "success": True,
        "status": "healthy"
    })


@app.get("/tester")
def tester():
    """Serve the browser-based API tester."""

    tester_path = os.path.join(BASE_DIR, "tester.html")

    if not os.path.isfile(tester_path):
        return jsonify({
            "success": False,
            "error": "tester.html was not found. Add tester.html to the project."
        }), 404

    return send_from_directory(BASE_DIR, "tester.html")


@app.post("/video_info")
def video_info():
    try:
        data = request.get_json(silent=True) or {}
        yt = get_youtube(data.get("url"))

        return jsonify({
            "success": True,
            "title": yt.title,
            "author": yt.author,
            "length": yt.length,
            "views": yt.views,
            "description": yt.description,
            "publish_date": (
                yt.publish_date.isoformat()
                if yt.publish_date
                else None
            ),
            "thumbnail_url": yt.thumbnail_url
        })

    except ValueError as exc:
        return jsonify({
            "success": False,
            "error": str(exc)
        }), 400

    except Exception as exc:
        app.logger.exception("Video information request failed")

        return jsonify({
            "success": False,
            "error": str(exc)
        }), 500


@app.post("/available_resolutions")
def available_resolutions():
    try:
        data = request.get_json(silent=True) or {}
        yt = get_youtube(data.get("url"))

        streams = yt.streams.filter(
            progressive=True,
            file_extension="mp4"
        )

        resolutions = sorted(
            {
                stream.resolution
                for stream in streams
                if stream.resolution
            },
            key=lambda value: int(value.rstrip("p"))
        )

        return jsonify({
            "success": True,
            "resolutions": resolutions
        })

    except ValueError as exc:
        return jsonify({
            "success": False,
            "error": str(exc)
        }), 400

    except Exception as exc:
        app.logger.exception("Resolution request failed")

        return jsonify({
            "success": False,
            "error": str(exc)
        }), 500


@app.post("/download/<resolution>")
def download_video(resolution):
    if not re.fullmatch(r"\d{3,4}p", resolution):
        return jsonify({
            "success": False,
            "error": "Invalid resolution."
        }), 400

    temp_path = None

    try:
        data = request.get_json(silent=True) or {}
        yt = get_youtube(data.get("url"))

        stream = yt.streams.filter(
            progressive=True,
            file_extension="mp4",
            resolution=resolution
        ).first()

        if stream is None:
            return jsonify({
                "success": False,
                "error": (
                    f"{resolution} progressive MP4 is unavailable."
                )
            }), 404

        # Create a temporary MP4 file.
        with tempfile.NamedTemporaryFile(
            suffix=".mp4",
            delete=False
        ) as temp_file:
            temp_path = temp_file.name

        # Download the selected video stream.
        stream.download(
            output_path=os.path.dirname(temp_path),
            filename=os.path.basename(temp_path)
        )

        safe_title = re.sub(
            r'[\\/*?:"<>|]',
            "",
            yt.title
        ).strip()[:100] or "youtube_video"

        download_name = f"{safe_title}_{resolution}.mp4"

        @after_this_request
        def cleanup(response):
            # Schedule temporary file cleanup after the response.
            # Streaming responses may need the file until transmission ends.
            try:
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                app.logger.warning(
                    "Temporary file cleanup failed: %s",
                    temp_path
                )

            return response

        return send_file(
            temp_path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=download_name
        )

    except ValueError as exc:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

        return jsonify({
            "success": False,
            "error": str(exc)
        }), 400

    except Exception as exc:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

        app.logger.exception("Video download failed")

        return jsonify({
            "success": False,
            "error": str(exc)
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )