FROM python:3.12-alpine3.21 AS repo_meta

WORKDIR /repo
COPY . .
RUN python - <<'PY'
from pathlib import Path
from urllib.parse import urlparse

config_path = Path(".git/config")
owner = ""

if config_path.exists():
    origin_url = None
    in_origin = False
    for raw_line in config_path.read_text().splitlines():
        line = raw_line.strip()
        if line.startswith('[remote "origin"]'):
            in_origin = True
            continue
        if line.startswith("[") and in_origin:
            in_origin = False
        if in_origin and line.startswith("url"):
            _, value = line.split("=", 1)
            origin_url = value.strip()
            break

    if origin_url:
        value = origin_url.strip()
        if value.startswith("git@") and ":" in value:
            value = value.split(":", 1)[1]
        parsed = urlparse(value)
        repo_path = parsed.path if parsed.netloc else value
        repo_path = repo_path.strip("/")
        if repo_path.endswith(".git"):
            repo_path = repo_path[:-4]
        if repo_path:
            owner = repo_path.split("/", 1)[0]

Path("/repo_owner").write_text(owner)
PY

FROM python:3.12-alpine3.21

# https://stackoverflow.com/questions/58701233/docker-logs-erroneously-appears-empty-until-container-stops
ENV PYTHONUNBUFFERED=1

# Define build argument with default value
ARG VERSION=dev
ARG COMMIT_SHA=unknown
# Set it as an environment variable
ENV VERSION=$VERSION
ENV COMMIT_SHA=$COMMIT_SHA

COPY ./requirements.txt /requirements.txt
COPY ./entrypoint.sh /entrypoint.sh
COPY ./supervisord.conf /etc/supervisord.conf
COPY ./nginx.conf /etc/nginx/nginx.conf
# Generate a copy of the nginx config with IPv6 support.
RUN sed 's/listen 8000;/listen 8000; listen [::]:8000;/' /etc/nginx/nginx.conf > /etc/nginx/nginx.ipv6.conf

WORKDIR /yamtrack

RUN apk add --no-cache nginx shadow \
    && pip install --no-cache-dir -r /requirements.txt \
    && pip install --no-cache-dir supervisor==4.3.0 \
    && rm -rf /root/.cache /tmp/* \
    && find /usr/local -type d -name __pycache__ -exec rm -rf {} + \
    && chmod +x /entrypoint.sh \
    # create user abc for later PUID/PGID mapping
    && useradd -U -M -s /bin/sh abc \
    # Create required nginx directories and set permissions
    && mkdir -p /var/log/nginx \
    && mkdir -p /var/lib/nginx/body

COPY --from=repo_meta /repo_owner /etc/yamtrack/fork_owner

# Django app
COPY src ./
RUN python manage.py collectstatic --noinput

EXPOSE 8000

CMD ["/entrypoint.sh"]

HEALTHCHECK --interval=45s --timeout=15s --start-period=30s --retries=5 \
  CMD wget --no-verbose --tries=1 --spider http://127.0.0.1:8000/health/ || exit 1
