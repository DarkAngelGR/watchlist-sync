#!/usr/bin/env python3
"""
Orama Server: everything around the Orama TV app, in one container.

  port 8789  watchlist sync, TV endpoints (Orama), converter (app.py, convert.py)
  port 8765  dashboard: service status, CPU/RAM, container updates (dash/)
  port 8790  TV app updates: installs new Orama releases on the TV (installer.py)

The ports and endpoints are the ones the four separate containers used, so
Orama on the TV, bookmarks and Sonarr/Radarr webhooks keep working. Parts
can be switched off with ORAMA_PARTS (default "sync,dashboard,installer").
"""
import logging
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("orama-server")


def start_dashboard():
    cfg = os.environ.get("MEDIADASH_CONFIG", "/config/config.yaml")
    if not os.path.exists(cfg):
        log.warning("Dashboard off: no %s (mount your mediadash config.yaml there)", cfg)
        return
    sys.path.insert(0, os.path.join(HERE, "dash"))
    import uvicorn
    import main as dash   # dash/main.py
    server = uvicorn.Server(uvicorn.Config(dash.app, host="0.0.0.0", port=int(os.environ.get("DASH_PORT", "8765")),
                                           access_log=False, log_level="warning"))
    threading.Thread(target=server.run, daemon=True, name="dashboard").start()
    log.info("Dashboard on port %s", server.config.port)


def start_installer():
    data = os.environ.get("DATA_DIR", "/data")
    os.environ.setdefault("INSTALLER_DATA_DIR", os.path.join(data, "installer"))
    os.environ.setdefault("INSTALLER_PORT", "8790")
    if os.environ.get("UI_PASSWORD"):
        os.environ.setdefault("INSTALLER_PASSWORD", os.environ["UI_PASSWORD"])
    import installer
    threading.Thread(target=installer.main, daemon=True, name="installer").start()


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)-7s %(message)s")
    parts = {p.strip() for p in os.environ.get("ORAMA_PARTS", "sync,dashboard,installer").split(",") if p.strip()}
    if "dashboard" in parts:
        try:
            start_dashboard()
        except Exception:
            log.exception("Dashboard failed to start")
    if "installer" in parts:
        try:
            start_installer()
        except Exception:
            log.exception("TV app updates failed to start")
    import app       # watchlist sync + TV endpoints + converter; runs in this thread
    if "sync" in parts:
        app.main()
    else:
        threading.Event().wait()


if __name__ == "__main__":
    main()
