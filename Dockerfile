# Two images from one folder:
#   --target watchlist-sync   the watchlist sync on its own (small, as before)
#   --target orama-server     everything around the Orama TV app (default):
#                             sync + TV endpoints, converter, dashboard, TV app updates

# ---------------------------------------------------------------- watchlist-sync
FROM python:3.12-slim-bookworm AS watchlist-sync
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
RUN pip install --no-cache-dir "requests>=2.32.4,<3"
COPY app.py convert.py ui.html ./
VOLUME /data
EXPOSE 8789
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8789'))" || exit 1
CMD ["python", "app.py"]

# ---------------------------------------------------------------- orama-server
FROM python:3.12-slim-bookworm AS orama-server
ARG TARGETARCH
ARG DOVI_TOOL_VERSION=2.1.2
# Apps2Samsung release with the command-line tool (signs and installs Orama on the TV)
ARG A2S_VERSION=latest
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data SELF_CONTAINER=orama-server

# ffmpeg (DTS → E-AC3), mkvtoolnix (Dolby Vision 7 → 8.1), and jellyfin-ffmpeg
# with libplacebo + Vulkan + Intel drivers (Dolby Vision 5 → HDR10).
RUN set -eu; \
    sed -i 's/^Components: main$/Components: main non-free non-free-firmware/' /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg ffmpeg mkvtoolnix; \
    curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key | gpg --dearmor -o /usr/share/keyrings/jellyfin.gpg; \
    echo "deb [signed-by=/usr/share/keyrings/jellyfin.gpg] https://repo.jellyfin.org/debian bookworm main" > /etc/apt/sources.list.d/jellyfin.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends jellyfin-ffmpeg7 mesa-vulkan-drivers libvulkan1; \
    if [ "$TARGETARCH" = amd64 ]; then apt-get install -y --no-install-recommends intel-media-va-driver-non-free; fi; \
    apt-get purge -y gnupg; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*

# dovi_tool
RUN set -eu; \
    case "$TARGETARCH" in amd64) T=x86_64-unknown-linux-musl ;; arm64) T=aarch64-unknown-linux-musl ;; *) echo "unsupported" >&2; exit 1 ;; esac; \
    curl -fsSL "https://github.com/quietvoid/dovi_tool/releases/download/${DOVI_TOOL_VERSION}/dovi_tool-${DOVI_TOOL_VERSION}-${T}.tar.gz" \
      | tar -xz -C /usr/local/bin dovi_tool; \
    dovi_tool --version

# Apps2Samsung command-line tool (as in the Orama installer)
RUN set -eu; \
    case "$TARGETARCH" in amd64) RID=linux-x64 ;; arm64) RID=linux-arm64 ;; *) echo "unsupported" >&2; exit 1 ;; esac; \
    if [ "$A2S_VERSION" = latest ]; then API="https://api.github.com/repos/Apps2Samsung/Apps2Samsung/releases?per_page=30"; \
    else API="https://api.github.com/repos/Apps2Samsung/Apps2Samsung/releases/tags/$A2S_VERSION"; fi; \
    curl -fsSL "$API" -o /tmp/r.json; \
    URL=$(RID=$RID python3 -c "import json,os; d=json.load(open('/tmp/r.json')); rels=d if isinstance(d,list) else [d]; rid=os.environ['RID']; u=[a['browser_download_url'] for r in rels if not r.get('draft') for a in r.get('assets',[]) if '-cli-' in a['name'] and a['name'].endswith('-'+rid+'.tar.gz')]; print(u[0] if u else '')"); \
    rm /tmp/r.json; [ -n "$URL" ]; \
    mkdir -p /opt/apps2samsung; curl -fsSL "$URL" | tar -xz -C /opt/apps2samsung; \
    chmod +x /opt/apps2samsung/apps2samsung-cli; ln -s /opt/apps2samsung/apps2samsung-cli /usr/local/bin/apps2samsung-cli

# docker CLI + compose plugin (the dashboard updates containers with them)
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:27-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py app.py convert.py installer.py ui.html ./
COPY dash/ ./dash/
VOLUME /data
EXPOSE 8789 8765 8790
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8789/health')" || exit 1
CMD ["python", "server.py"]
