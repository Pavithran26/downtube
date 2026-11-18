# ReClip

A self-hosted media workspace built with Flask, yt-dlp, and FFmpeg. The included UI uses a soft-neumorphic palette: midnight teal #0b3048, soft pearl #f2f0e9, and neon lime #b9f12f.

## Features

- MP4 video and MP3 audio output
- Format and resolution selection
- Multiple links and small playlist batches
- Responsive, dependency-free frontend
- Flask API served with Gunicorn
- Automatic removal of temporary downloads after 30 minutes
- Request limits, single active download, URL validation, and a 1 GB per-file limit
- Intended for content the operator owns or is authorized to download

## Stack

- Python 3.12, Flask, Gunicorn
- yt-dlp with its default EJS components for YouTube support
- Deno JavaScript runtime for YouTube challenge solving
- FFmpeg for merging and audio extraction
- Nginx reverse proxy and HTTPS
- Ubuntu 24.04 LTS

## Local setup

On Ubuntu:

\`\`\`bash
sudo apt update
sudo apt install -y python3-venv ffmpeg
curl -fsSL https://deno.land/install.sh | sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
mkdir -p downloads
HOST=127.0.0.1 PORT=8899 python app.py
\`\`\`

Open http://127.0.0.1:8899.

## Production deployment notes

Run Gunicorn as a non-root account and bind it only to 127.0.0.1:8899. Place Nginx in front of it. Protect the site with HTTP Basic Authentication or another strong access-control layer, terminate HTTPS at Nginx, and keep port 8899 private. The included EC2 deployment uses AWS Systems Manager Session Manager for administration; it does not require a public SSH rule.

Keep the downloads directory on a dedicated writable path. Temporary files are deleted after the configured retention period.

## Security and lawful use

- Only accept public HTTP/HTTPS URLs whose host resolves to globally routable addresses. Private, loopback, link-local, and reserved addresses are rejected.
- Keep the app bound to localhost behind an authenticated reverse proxy.
- Do not expose this service as an unrestricted public downloader.
- Use only media you own, have permission to download, or are otherwise legally entitled to save. Do not bypass DRM, paywalls, private access controls, or platform restrictions. Respect each platform's terms and applicable copyright law.

## Docker

The repository includes a Dockerfile and Compose file for local experimentation. For production, place the container behind an authenticated HTTPS reverse proxy, restrict its network access, and persist only the temporary downloads volume.

## License

MIT. See LICENSE.
