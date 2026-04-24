#!/usr/bin/env python3
"""
InkyBerry Web Dashboard — Flask Backend
Lightweight API server for the local-network configuration UI.
Designed for Raspberry Pi Zero 2 W (512MB RAM).
"""

import os
import sys
import json
import time
import yaml
import shutil
import signal
import logging
import subprocess
import threading
from datetime import datetime
from functools import wraps

from flask import (
    Flask, jsonify, request, send_from_directory,
    send_file, Response, abort
)
from werkzeug.utils import secure_filename

# Project root (one level up from web/)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
PHOTOS_DIR = os.path.expanduser(
    "~/inkyberry/photos"
)  # default; overridden from config
PREVIEW_PATH = os.path.join(ROOT, "preview.png")
SCREENSHOT_DIR = os.path.join(ROOT, "screenshots")

# Ensure screenshot dir exists
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

logger = logging.getLogger("inkyberry.web")

app = Flask(
    __name__,
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
    static_url_path="/static",
)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB upload limit


# ─── Helpers ───────────────────────────────────────────────────

def load_config():
    """Load config.yaml."""
    try:
        with open(CONFIG_PATH, "r") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return {}


def save_config(config):
    """Write config.yaml."""
    try:
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        return True
    except Exception as e:
        logger.error(f"Failed to save config: {e}")
        return False


def get_photos_dir():
    """Get photos directory from config."""
    cfg = load_config()
    d = cfg.get("photo_frame", {}).get("directory", "~/inkyberry/photos")
    return os.path.expanduser(d)


def run_cmd(cmd, timeout=10):
    """Run a shell command, return stdout or None on error."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip()
    except Exception:
        return None


# ─── Static / SPA ──────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ─── API: Config ───────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["PUT"])
def api_put_config():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400
    if save_config(data):
        return jsonify({"ok": True})
    return jsonify({"error": "Failed to save"}), 500


# ─── API: Plugins ──────────────────────────────────────────────

def scan_installed_plugins():
    """Scan plugins/ directory for installed plugins."""
    plugins_dir = os.path.join(ROOT, "plugins")
    installed = []
    if not os.path.isdir(plugins_dir):
        return installed
    for name in sorted(os.listdir(plugins_dir)):
        plugin_py = os.path.join(plugins_dir, name, "plugin.py")
        if os.path.isfile(plugin_py):
            # Try to read plugin.json if it exists
            pjson_path = os.path.join(plugins_dir, name, "plugin.json")
            meta = {}
            if os.path.isfile(pjson_path):
                try:
                    with open(pjson_path) as f:
                        meta = json.load(f)
                except Exception:
                    pass
            installed.append({
                "name": name,
                "has_plugin_json": os.path.isfile(pjson_path),
                "meta": meta,
            })
    return installed


@app.route("/api/plugins", methods=["GET"])
def api_plugins():
    cfg = load_config()
    active = cfg.get("plugins", {}).get("active", [])
    installed = scan_installed_plugins()
    return jsonify({
        "active": active,
        "installed": installed,
        "rotation_interval": cfg.get("plugins", {}).get("rotation_interval", 0),
    })


@app.route("/api/plugins/active", methods=["PUT"])
def api_set_active_plugins():
    """Set the active plugin list (reorder, enable/disable)."""
    data = request.get_json()
    active = data.get("active", [])
    cfg = load_config()
    if "plugins" not in cfg:
        cfg["plugins"] = {}
    cfg["plugins"]["active"] = active
    if save_config(cfg):
        return jsonify({"ok": True})
    return jsonify({"error": "Failed to save"}), 500


@app.route("/api/plugins/<name>/settings", methods=["GET"])
def api_plugin_settings(name):
    """Get per-plugin settings from config.yaml."""
    cfg = load_config()
    settings = cfg.get(name, {})
    # Also check for plugin.json schema
    pjson_path = os.path.join(ROOT, "plugins", name, "plugin.json")
    schema = None
    if os.path.isfile(pjson_path):
        try:
            with open(pjson_path) as f:
                schema = json.load(f)
        except Exception:
            pass
    return jsonify({"settings": settings, "schema": schema})


@app.route("/api/plugins/<name>/settings", methods=["PUT"])
def api_set_plugin_settings(name):
    """Update per-plugin settings in config.yaml."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400
    cfg = load_config()
    cfg[name] = data.get("settings", data)
    if save_config(cfg):
        return jsonify({"ok": True})
    return jsonify({"error": "Failed to save"}), 500


# ─── API: Display / Preview ───────────────────────────────────

@app.route("/api/display/preview")
def api_preview():
    """Serve the latest display screenshot."""
    # Check for the most recent preview
    if os.path.isfile(PREVIEW_PATH):
        return send_file(
            PREVIEW_PATH,
            mimetype="image/png",
            max_age=0,
        )
    return jsonify({"error": "No preview available"}), 404


@app.route("/api/display/preview/meta")
def api_preview_meta():
    """Get metadata about the current preview."""
    if os.path.isfile(PREVIEW_PATH):
        stat = os.stat(PREVIEW_PATH)
        return jsonify({
            "exists": True,
            "modified": stat.st_mtime,
            "modified_iso": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "size": stat.st_size,
        })
    return jsonify({"exists": False})


@app.route("/api/display/refresh", methods=["POST"])
def api_refresh_display():
    """Trigger a display refresh by sending SIGUSR1 to the main process."""
    try:
        # Find the inkyberry main.py process
        pid = run_cmd("pgrep -f 'python.*main.py'")
        if pid:
            os.kill(int(pid.split()[0]), signal.SIGUSR1)
            return jsonify({"ok": True, "pid": int(pid.split()[0])})
        return jsonify({"error": "InkyBerry process not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/display/settings", methods=["GET"])
def api_display_settings():
    cfg = load_config()
    return jsonify(cfg.get("display", {}))


@app.route("/api/display/settings", methods=["PUT"])
def api_set_display_settings():
    data = request.get_json()
    cfg = load_config()
    cfg["display"] = data
    if save_config(cfg):
        return jsonify({"ok": True})
    return jsonify({"error": "Failed to save"}), 500


# ─── API: Photos ───────────────────────────────────────────────

ALLOWED_PHOTO_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".heic", ".heif"}


@app.route("/api/photos", methods=["GET"])
def api_photos():
    """List all photos."""
    photo_dir = get_photos_dir()
    if not os.path.isdir(photo_dir):
        return jsonify({"photos": [], "total": 0, "directory": photo_dir})

    photos = []
    for f in sorted(os.listdir(photo_dir)):
        ext = os.path.splitext(f)[1].lower()
        if ext in ALLOWED_PHOTO_EXT:
            fpath = os.path.join(photo_dir, f)
            stat = os.stat(fpath)
            photos.append({
                "name": f,
                "size": stat.st_size,
                "modified": stat.st_mtime,
            })

    # Total size
    total_size = sum(p["size"] for p in photos)

    return jsonify({
        "photos": photos,
        "total": len(photos),
        "total_size": total_size,
        "directory": photo_dir,
    })


@app.route("/api/photos/thumb/<name>")
def api_photo_thumb(name):
    """Serve a photo thumbnail."""
    photo_dir = get_photos_dir()
    fpath = os.path.join(photo_dir, secure_filename(name))
    if not os.path.isfile(fpath):
        abort(404)
    return send_file(fpath, max_age=3600)


@app.route("/api/photos/upload", methods=["POST"])
def api_upload_photos():
    """Upload one or more photos."""
    photo_dir = get_photos_dir()
    os.makedirs(photo_dir, exist_ok=True)
    uploaded = []
    for f in request.files.getlist("photos"):
        if f and f.filename:
            fname = secure_filename(f.filename)
            ext = os.path.splitext(fname)[1].lower()
            if ext not in ALLOWED_PHOTO_EXT:
                continue
            dest = os.path.join(photo_dir, fname)
            f.save(dest)
            uploaded.append(fname)
    return jsonify({"uploaded": uploaded, "count": len(uploaded)})


@app.route("/api/photos/<name>", methods=["DELETE"])
def api_delete_photo(name):
    """Delete a photo."""
    photo_dir = get_photos_dir()
    fpath = os.path.join(photo_dir, secure_filename(name))
    if os.path.isfile(fpath):
        os.remove(fpath)
        return jsonify({"ok": True})
    return jsonify({"error": "Not found"}), 404


# ─── API: System ───────────────────────────────────────────────

@app.route("/api/system/stats")
def api_system_stats():
    """Get live system statistics."""
    stats = {}

    # CPU usage
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            stats["load_1m"] = float(parts[0])
            stats["load_5m"] = float(parts[1])
            stats["load_15m"] = float(parts[2])
        # Approximate CPU % from load average (4 cores)
        stats["cpu_percent"] = min(100, round(stats["load_1m"] / 4 * 100))
    except Exception:
        stats["cpu_percent"] = 0
        stats["load_1m"] = 0

    # Memory
    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                k, v = line.split(":")
                meminfo[k.strip()] = int(v.strip().split()[0])  # kB
            total = meminfo.get("MemTotal", 0) // 1024  # MB
            avail = meminfo.get("MemAvailable", 0) // 1024
            stats["mem_total_mb"] = total
            stats["mem_used_mb"] = total - avail
            stats["mem_percent"] = round((total - avail) / total * 100) if total else 0
    except Exception:
        stats["mem_total_mb"] = 512
        stats["mem_used_mb"] = 0
        stats["mem_percent"] = 0

    # CPU temperature
    try:
        temp = run_cmd("cat /sys/class/thermal/thermal_zone0/temp")
        if temp:
            stats["cpu_temp"] = round(int(temp) / 1000, 1)
        else:
            stats["cpu_temp"] = 0
    except Exception:
        stats["cpu_temp"] = 0

    # Disk
    try:
        st = os.statvfs("/")
        total_gb = (st.f_blocks * st.f_frsize) / (1024 ** 3)
        free_gb = (st.f_bavail * st.f_frsize) / (1024 ** 3)
        used_gb = total_gb - free_gb
        stats["disk_total_gb"] = round(total_gb, 1)
        stats["disk_used_gb"] = round(used_gb, 1)
        stats["disk_percent"] = round(used_gb / total_gb * 100) if total_gb else 0
    except Exception:
        stats["disk_total_gb"] = 0
        stats["disk_used_gb"] = 0
        stats["disk_percent"] = 0

    # Uptime
    try:
        with open("/proc/uptime") as f:
            uptime_sec = float(f.read().split()[0])
            days = int(uptime_sec // 86400)
            hours = int((uptime_sec % 86400) // 3600)
            mins = int((uptime_sec % 3600) // 60)
            stats["uptime_seconds"] = int(uptime_sec)
            stats["uptime_str"] = f"{days}d {hours}h {mins}m"
    except Exception:
        stats["uptime_str"] = "unknown"

    # Hostname & IP
    try:
        stats["hostname"] = run_cmd("hostname") or "inkyberry"
        stats["ip"] = run_cmd("hostname -I")
        if stats["ip"]:
            stats["ip"] = stats["ip"].split()[0]
        else:
            stats["ip"] = "unknown"
    except Exception:
        stats["hostname"] = "inkyberry"
        stats["ip"] = "unknown"

    # Swap
    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                k, v = line.split(":")
                meminfo[k.strip()] = int(v.strip().split()[0])
            swap_total = meminfo.get("SwapTotal", 0) // 1024
            swap_free = meminfo.get("SwapFree", 0) // 1024
            stats["swap_used_mb"] = swap_total - swap_free
            stats["swap_total_mb"] = swap_total
    except Exception:
        stats["swap_used_mb"] = 0
        stats["swap_total_mb"] = 0

    # Timezone
    try:
        stats["timezone"] = run_cmd("cat /etc/timezone") or "UTC"
    except Exception:
        stats["timezone"] = "UTC"

    # Python version
    stats["python_version"] = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    # Service status
    svc_status = run_cmd("systemctl is-active inkyberry.service")
    stats["service_status"] = svc_status or "unknown"

    return jsonify(stats)


@app.route("/api/system/service", methods=["POST"])
def api_service_control():
    """Start/stop/restart the inkyberry systemd service."""
    data = request.get_json()
    action = data.get("action", "")
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": "Invalid action"}), 400
    try:
        result = subprocess.run(
            ["sudo", "systemctl", action, "inkyberry.service"],
            capture_output=True, text=True, timeout=30
        )
        return jsonify({
            "ok": result.returncode == 0,
            "action": action,
            "stderr": result.stderr.strip() if result.returncode != 0 else "",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/system/reboot", methods=["POST"])
def api_reboot():
    """Reboot the Pi."""
    try:
        subprocess.Popen(["sudo", "reboot"])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/system/shutdown", methods=["POST"])
def api_shutdown():
    """Shutdown the Pi."""
    try:
        subprocess.Popen(["sudo", "shutdown", "-h", "now"])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── API: Logs (SSE) ──────────────────────────────────────────

@app.route("/api/logs/stream")
def api_logs_stream():
    """Stream journalctl logs via Server-Sent Events."""
    level = request.args.get("level", "")
    plugin = request.args.get("plugin", "")

    cmd = ["journalctl", "-u", "inkyberry.service", "-f", "-n", "50", "--no-pager", "-o", "short-iso"]
    if level:
        priority_map = {"error": "3", "warn": "4", "info": "6"}
        p = priority_map.get(level.lower(), "")
        if p:
            cmd.extend(["-p", p])

    def generate():
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            while True:
                line = proc.stdout.readline()
                if not line:
                    # Send heartbeat
                    yield f"data: {json.dumps({'heartbeat': True})}\n\n"
                    time.sleep(2)
                    continue
                # Optional client-side plugin filter
                if plugin and f"[{plugin}]" not in line and plugin not in line:
                    continue
                yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
        except GeneratorExit:
            proc.kill()
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/logs/recent")
def api_logs_recent():
    """Get recent log lines (non-streaming)."""
    n = request.args.get("n", "50")
    output = run_cmd(f"journalctl -u inkyberry.service -n {n} --no-pager -o short-iso")
    if output:
        lines = output.split("\n")
        return jsonify({"lines": lines})
    return jsonify({"lines": []})


# ─── API: Schedule ─────────────────────────────────────────────

@app.route("/api/schedule", methods=["GET"])
def api_get_schedule():
    cfg = load_config()
    return jsonify(cfg.get("schedule", {"enabled": False, "rules": []}))


@app.route("/api/schedule", methods=["PUT"])
def api_set_schedule():
    data = request.get_json()
    cfg = load_config()
    cfg["schedule"] = data
    if save_config(cfg):
        return jsonify({"ok": True})
    return jsonify({"error": "Failed to save"}), 500


# ─── API: Buttons ──────────────────────────────────────────────

@app.route("/api/buttons", methods=["GET"])
def api_get_buttons():
    cfg = load_config()
    return jsonify(cfg.get("buttons", {"A": 5, "B": 6, "C": 16, "D": 24}))


@app.route("/api/buttons", methods=["PUT"])
def api_set_buttons():
    data = request.get_json()
    cfg = load_config()
    cfg["buttons"] = data
    if save_config(cfg):
        return jsonify({"ok": True})
    return jsonify({"error": "Failed to save"}), 500


# ─── Run ───────────────────────────────────────────────────────

def create_app():
    """Factory for use with gunicorn or direct run."""
    return app


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Run on all interfaces so it's accessible on local network
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
