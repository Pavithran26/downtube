import glob
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", BASE_DIR / "downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

JOB_TTL = int(os.environ.get("JOB_TTL_SECONDS", "1800"))
MAX_DOWNLOAD_BYTES = int(os.environ.get("MAX_DOWNLOAD_BYTES", str(1024 ** 3)))
MAX_PLAYLIST_ITEMS = 10
MAX_ACTIVE_DOWNLOADS = 1
DOWNLOAD_TIMEOUT = 1800
JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
jobs = {}
jobs_lock = threading.RLock()
rate_lock = threading.Lock()
rate_buckets = defaultdict(deque)


def client_ip():
    return request.remote_addr or "unknown"


def rate_limit(bucket, limit, window):
    now = time.time()
    key = (client_ip(), bucket)
    with rate_lock:
        history = rate_buckets[key]
        while history and now - history[0] > window:
            history.popleft()
        if len(history) >= limit:
            return False
        history.append(now)
        return True


def cleanup_expired():
    cutoff = time.time() - JOB_TTL
    try:
        for path in DOWNLOAD_DIR.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass

    with jobs_lock:
        for job_id, job in list(jobs.items()):
            if job.get("created", 0) < cutoff and job.get("status") != "downloading":
                jobs.pop(job_id, None)


def validate_public_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Enter a valid URL (maximum 2048 characters).")

    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only public HTTP and HTTPS URLs are supported.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded credentials are not allowed.")

    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "metadata.google.internal"} or host.endswith((".localhost", ".local", ".internal", ".test")):
        raise ValueError("Private or local network addresses are not allowed.")

    try:
        literal_ip = ipaddress.ip_address(host)
        resolved = {literal_ip}
    except ValueError:
        try:
            results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            resolved = {ipaddress.ip_address(item[4][0].split("%")[0]) for item in results}
        except (OSError, ValueError):
            raise ValueError("The URL host could not be resolved.")

    if not resolved or any(not address.is_global for address in resolved):
        raise ValueError("Private, local, and reserved network addresses are not allowed.")

    return value


def request_data():
    if not request.is_json:
        return None, (jsonify({"error": "Send JSON content."}), 415)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({"error": "Invalid JSON request."}), 400)
    return data, None


def parse_ytdlp_json(stdout):
    for line in stdout.splitlines():
        line = line.strip()
        if line:
            return json.loads(line)
    raise ValueError("The media source returned no information.")


def active_download_count():
    return sum(1 for job in jobs.values() if job.get("status") == "downloading")


@app.before_request
def request_guards():
    cleanup_expired()
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() != request.host.lower():
            abort(403)


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/healthz")
def health():
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    if not rate_limit("info", 20, 60):
        return jsonify({"error": "Too many requests. Please wait a minute."}), 429
    data, error = request_data()
    if error:
        return error
    try:
        url = validate_public_url(data.get("url", ""))
        command = ["yt-dlp", "--no-playlist", "--no-warnings", "--socket-timeout", "20", "-j", url]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
        if result.returncode != 0:
            lines = result.stderr.strip().splitlines()
            return jsonify({"error": lines[-1][-240:] if lines else "Could not read this media URL."}), 400

        info = parse_ytdlp_json(result.stdout)
        best_by_height = {}
        for item in info.get("formats", []):
            height = item.get("height")
            if height and item.get("vcodec", "none") != "none":
                bitrate = item.get("tbr") or 0
                if height not in best_by_height or bitrate > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = item

        formats = [
            {"id": item["format_id"], "label": str(height) + "p", "height": height}
            for height, item in best_by_height.items()
            if item.get("format_id") and re.fullmatch(r"[A-Za-z0-9_.+-]{1,80}", str(item["format_id"]))
        ]
        formats.sort(key=lambda item: item["height"], reverse=True)

        thumbnail = info.get("thumbnail") or ""
        if not isinstance(thumbnail, str) or not thumbnail.startswith(("https://", "http://")):
            thumbnail = ""

        return jsonify({
            "title": str(info.get("title") or "")[:300],
            "thumbnail": thumbnail,
            "duration": info.get("duration"),
            "uploader": str(info.get("uploader") or "")[:120],
            "formats": formats[:12],
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "The information request timed out. Try again."}), 408
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        app.logger.exception("Media information lookup failed")
        return jsonify({"error": "Unable to inspect this media URL."}), 500


@app.route("/api/playlist", methods=["POST"])
def get_playlist_info():
    if not rate_limit("playlist", 5, 60):
        return jsonify({"error": "Too many playlist requests. Please wait a minute."}), 429
    data, error = request_data()
    if error:
        return error
    try:
        url = validate_public_url(data.get("url", ""))
        command = [
            "yt-dlp", "--flat-playlist", "--playlist-end", str(MAX_PLAYLIST_ITEMS),
            "--no-warnings", "--socket-timeout", "20", "-J", url
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
        if result.returncode != 0:
            lines = result.stderr.strip().splitlines()
            return jsonify({"error": lines[-1][-240:] if lines else "Could not read this playlist."}), 400
        info = json.loads(result.stdout)
        urls = []
        for entry in (info.get("entries") or [])[:MAX_PLAYLIST_ITEMS]:
            candidate = entry.get("webpage_url") or entry.get("original_url")
            if candidate:
                try:
                    urls.append(validate_public_url(candidate))
                except ValueError:
                    continue
        return jsonify({"urls": urls, "truncated": len(info.get("entries") or []) >= MAX_PLAYLIST_ITEMS})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "The playlist lookup timed out."}), 408
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        app.logger.exception("Playlist lookup failed")
        return jsonify({"error": "Unable to inspect this playlist."}), 500


def run_download(job_id, url, format_choice, format_id, title):
    output_template = str(DOWNLOAD_DIR / (job_id + ".%(ext)s"))
    command = [
        "yt-dlp", "--no-playlist", "--no-warnings", "--socket-timeout", "20",
        "--retries", "2", "--max-filesize", "1G", "--no-overwrites",
        "-o", output_template,
    ]
    if format_choice == "audio":
        command += ["-x", "--audio-format", "mp3", "--audio-quality", "5"]
    elif format_id:
        command += ["-f", format_id + "+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        command += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]
    command.append(url)

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT, check=False)
        if result.returncode != 0:
            lines = result.stderr.strip().splitlines()
            with jobs_lock:
                jobs[job_id].update(status="error", error=(lines[-1][-240:] if lines else "Download failed."))
            return

        files = [Path(name) for name in glob.glob(str(DOWNLOAD_DIR / (job_id + ".*"))) if Path(name).is_file()]
        if not files:
            with jobs_lock:
                jobs[job_id].update(status="error", error="Download finished but no output file was found.")
            return

        preferred_ext = ".mp3" if format_choice == "audio" else ".mp4"
        chosen = next((path for path in files if path.suffix.lower() == preferred_ext), files[0])
        for path in files:
            if path != chosen:
                path.unlink(missing_ok=True)

        safe_title = "".join(ch for ch in (title or "") if ch not in r'\/:*?"<>|')[:100].strip()
        filename = safe_title + chosen.suffix if safe_title else chosen.name
        with jobs_lock:
            jobs[job_id].update(status="done", file=str(chosen), filename=filename)
    except subprocess.TimeoutExpired:
        for path in DOWNLOAD_DIR.glob(job_id + ".*"):
            path.unlink(missing_ok=True)
        with jobs_lock:
            jobs[job_id].update(status="error", error="Download timed out (30-minute limit).")
    except Exception:
        app.logger.exception("Download job failed")
        with jobs_lock:
            jobs[job_id].update(status="error", error="Download failed unexpectedly.")


@app.route("/api/download", methods=["POST"])
def start_download():
    if not rate_limit("download", 5, 3600):
        return jsonify({"error": "Download limit reached. Try again later."}), 429
    data, error = request_data()
    if error:
        return error
    try:
        url = validate_public_url(data.get("url", ""))
        format_choice = data.get("format", "video")
        if format_choice not in ("video", "audio"):
            return jsonify({"error": "Choose MP4 video or MP3 audio."}), 400
        format_id = data.get("format_id")
        if format_id and (not isinstance(format_id, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,80}", format_id)):
            return jsonify({"error": "Invalid quality selection."}), 400

        if shutil.disk_usage(DOWNLOAD_DIR).free < 2 * 1024 ** 3:
            return jsonify({"error": "Server storage is low. Try again later."}), 507

        with jobs_lock:
            if active_download_count() >= MAX_ACTIVE_DOWNLOADS:
                return jsonify({"error": "One download is already running. Please wait until it finishes."}), 429
            job_id = uuid.uuid4().hex[:12]
            jobs[job_id] = {
                "status": "downloading", "url": url, "title": str(data.get("title") or "")[:300],
                "created": time.time(),
            }
        worker = threading.Thread(
            target=run_download,
            args=(job_id, url, format_choice, format_id, str(data.get("title") or "")),
            daemon=True,
        )
        worker.start()
        return jsonify({"job_id": job_id})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/status/<job_id>")
def check_status(job_id):
    if not JOB_ID_RE.fullmatch(job_id):
        return jsonify({"error": "Invalid job ID."}), 400
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found or expired."}), 404
        return jsonify({
            "status": job["status"],
            "error": job.get("error"),
            "filename": job.get("filename"),
            "expires_in_seconds": max(0, JOB_TTL - int(time.time() - job.get("created", time.time()))),
        })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    if not JOB_ID_RE.fullmatch(job_id):
        return jsonify({"error": "Invalid job ID."}), 400
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job.get("status") != "done":
            return jsonify({"error": "File not ready or expired."}), 404
        file_path = Path(job.get("file", "")).resolve()
        if file_path.parent != DOWNLOAD_DIR.resolve() or not file_path.is_file():
            return jsonify({"error": "Download file is no longer available."}), 404
        return send_file(
            file_path, as_attachment=True,
            download_name=job.get("filename", file_path.name),
            conditional=True,
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8899"))
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=port)
