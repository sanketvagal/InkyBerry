#!/usr/bin/env python3
"""
InkyBerry - E-Ink Display Manager for Raspberry Pi + Inky Impression
Main application: plugin loader, scheduler, button handler.
"""

import os
import sys
import time
import logging
import importlib
import threading
import signal
import yaml
from datetime import datetime

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("inkyberry")

# Project root
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from display import Display
from buttons import ButtonHandler


class InkyBerry:
    """Main application controller."""

    def __init__(self):
        self.config = self._load_config()
        self.display = Display(self.config)
        self.plugins = []
        self.current_plugin_index = 0
        self.show_overlay = False
        self._running = False
        self._update_lock = threading.Lock()
        self._rendering = threading.Event()  # set while e-ink is updating
        self._last_refresh = {}
        self._rotation_timer = None

        # Load plugins
        self._load_plugins()

        # Setup buttons
        self.button_handler = ButtonHandler(self.config, self._on_button)

    def _load_config(self):
        """Load configuration from config.yaml."""
        config_path = os.path.join(ROOT, "config.yaml")
        try:
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
            logger.info("Config loaded")
            return config
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            return {}

    def _load_plugins(self):
        """Dynamically load active plugins."""
        active = self.config.get("plugins", {}).get("active", [])
        plugin_dir = os.path.join(ROOT, "plugins")

        for name in active:
            plugin_path = os.path.join(plugin_dir, name, "plugin.py")
            if not os.path.exists(plugin_path):
                logger.warning(f"Plugin not found: {name} ({plugin_path})")
                continue

            try:
                spec = importlib.util.spec_from_file_location(
                    f"plugins.{name}", plugin_path
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                plugin = module.Plugin(self.config, self.display)
                self.plugins.append(plugin)
                logger.info(f"Loaded plugin: {plugin.name}")
            except Exception as e:
                logger.error(f"Error loading plugin '{name}': {e}")

        if not self.plugins:
            logger.warning("No plugins loaded!")

    def _on_button(self, button, long_press=False):
        """Handle button presses. Runs on GPIO thread — dispatches work to a worker."""
        if not self.plugins:
            return

        # Drop button if e-ink is still updating — prevents input queue buildup
        if self._rendering.is_set():
            logger.warning(f"Button {button} ignored — display is still rendering, dropping input")
            return

        # Claim the rendering lock immediately so any concurrent button press
        # that arrives before display.show() is called will still be dropped
        self._rendering.set()

        # Dispatch to worker thread so GPIO thread is freed immediately
        t = threading.Thread(
            target=self._handle_button, args=(button, long_press), daemon=True
        )
        t.start()

    def _handle_button(self, button, long_press):
        """Handle button action on a worker thread (off the GPIO thread)."""
        try:
            current = self.plugins[self.current_plugin_index]

            # Long press D = show IP / system info
            if button == "D" and long_press:
                self._show_system_info()
                return

            # Let the current plugin handle the button first
            if current.on_button(button):
                # Plugin handled it — re-render
                self._render_current()
                return

            # Default button actions
            if button == "A":
                # Previous plugin
                self.current_plugin_index = (
                    (self.current_plugin_index - 1) % len(self.plugins)
                )
                logger.info(f"Switched to: {self.plugins[self.current_plugin_index].name}")
                self._refresh_and_render()

            elif button == "B":
                # Next plugin
                self.current_plugin_index = (
                    (self.current_plugin_index + 1) % len(self.plugins)
                )
                logger.info(f"Switched to: {self.plugins[self.current_plugin_index].name}")
                self._refresh_and_render()

            elif button == "C":
                # Refresh current plugin
                logger.info("Manual refresh triggered")
                self._refresh_and_render()

            elif button == "D":
                # Toggle info overlay
                self.show_overlay = not self.show_overlay
                self._render_current()

        except Exception as e:
            logger.error(f"Error handling button {button}: {e}")
            self._rendering.clear()  # ensure flag is cleared on error

    def _refresh_and_render(self):
        """Update data and render the current plugin."""
        with self._update_lock:
            current = self.plugins[self.current_plugin_index]
            try:
                current.update_data()
                self._last_refresh[current.name] = datetime.now()
            except Exception as e:
                logger.error(f"Error updating {current.name}: {e}")
            self._render_current()

    def _render_current(self):
        """Render the current plugin to the display."""
        if not self.plugins:
            return

        current = self.plugins[self.current_plugin_index]

        # Write state file so web dashboard knows which plugin is active
        try:
            import json as _json
            with open("/tmp/inkyberry_state.json", "w") as _f:
                _json.dump({
                    "current_plugin": current.name,
                    "current_index": self.current_plugin_index,
                    "total_plugins": len(self.plugins),
                }, _f)
        except Exception:
            pass
        try:
            img = current.render()
            if img is None:
                logger.warning(f"{current.name} returned None image")
                return

            # Draw overlay if enabled
            if self.show_overlay:
                from PIL import ImageDraw
                draw = ImageDraw.Draw(img)
                last_update = self._last_refresh.get(current.name)
                update_str = last_update.strftime("%H:%M:%S") if last_update else "never"
                self.display.draw_info_overlay(
                    draw, current.name, self.current_plugin_index,
                    len(self.plugins), update_str, img=img
                )

            self._rendering.set()
            logger.info(f"Display render start — e-ink updating")
            try:
                self.display.show(img)
            finally:
                self._rendering.clear()
                logger.info(f"Display render complete — buttons re-enabled")

        except Exception as e:
            logger.error(f"Error rendering {current.name}: {e}")

    def _show_system_info(self):
        """Display system information (IP, uptime, etc.)."""
        from display import BLACK, WHITE, BLUE
        img, draw = self.display.create_canvas(bg_color=WHITE)
        self.display.draw_header(draw, "System Info", compact=True, img=img)

        y = 70
        font = self.display.get_font(22)
        bold = self.display.get_font(22, bold=True)

        # Get IP
        import subprocess
        try:
            ip = subprocess.check_output(
                ["hostname", "-I"], text=True
            ).strip().split()[0]
        except Exception:
            ip = "Unknown"

        # Get uptime
        try:
            with open("/proc/uptime", "r") as f:
                uptime_sec = float(f.readline().split()[0])
            hours = int(uptime_sec // 3600)
            mins = int((uptime_sec % 3600) // 60)
            uptime = f"{hours}h {mins}m"
        except Exception:
            uptime = "Unknown"

        # Get temp
        try:
            temp_str = subprocess.check_output(
                ["vcgencmd", "measure_temp"], text=True
            ).strip().replace("temp=", "")
        except Exception:
            temp_str = "Unknown"

        info_items = [
            ("Hostname", "inkyberry"),
            ("IP Address", ip),
            ("Uptime", uptime),
            ("CPU Temp", temp_str),
            ("Plugins", f"{len(self.plugins)} loaded"),
            ("Current", self.plugins[self.current_plugin_index].name if self.plugins else "None"),
        ]

        for label, value in info_items:
            draw.text((30, y), f"{label}:", fill=BLACK, font=font)
            draw.text((250, y), value, fill=BLACK, font=bold)
            y += 40

        y += 20
        hint_font = self.display.get_font(16)
        draw.text((30, y), "Press any button to return", fill=BLACK, font=hint_font)

        self._rendering.set()
        logger.info("Display render start — e-ink updating")
        try:
            self.display.show(img)
        finally:
            self._rendering.clear()
            logger.info("Display render complete — buttons re-enabled")

    def _schedule_rotation(self):
        """Auto-rotate plugins on a timer."""
        interval = self.config.get("plugins", {}).get("rotation_interval", 0)
        if interval <= 0:
            return

        def rotate():
            while self._running:
                time.sleep(interval)
                if self._running and self.plugins:
                    self.current_plugin_index = (
                        (self.current_plugin_index + 1) % len(self.plugins)
                    )
                    logger.info(
                        f"Auto-rotating to: "
                        f"{self.plugins[self.current_plugin_index].name}"
                    )
                    self._refresh_and_render()

        self._rotation_timer = threading.Thread(target=rotate, daemon=True)
        self._rotation_timer.start()

    def _schedule_refreshes(self):
        """Schedule per-plugin data refreshes based on their refresh_interval."""
        def refresh_loop():
            while self._running:
                time.sleep(30)  # Check every 30 seconds
                now = datetime.now()
                for i, plugin in enumerate(self.plugins):
                    last = self._last_refresh.get(plugin.name)
                    if last is None:
                        continue
                    elapsed = (now - last).total_seconds()
                    if elapsed >= plugin.refresh_interval:
                        logger.info(f"Auto-refreshing: {plugin.name}")
                        try:
                            plugin.update_data()
                            self._last_refresh[plugin.name] = now
                            # Re-render if it's the active plugin
                            if i == self.current_plugin_index:
                                self._render_current()
                        except Exception as e:
                            logger.error(f"Auto-refresh error for {plugin.name}: {e}")

        t = threading.Thread(target=refresh_loop, daemon=True)
        t.start()

    def run(self):
        """Main run loop."""
        logger.info("=" * 40)
        logger.info("  InkyBerry Starting")
        logger.info("=" * 40)

        if not self.plugins:
            logger.error("No plugins loaded. Exiting.")
            return

        self._running = True

        # Handle shutdown gracefully
        def shutdown(sig, frame):
            logger.info("Shutting down...")
            self._running = False

        # SIGUSR1 = web dashboard requests a refresh or plugin switch
        def on_refresh_signal(sig, frame):
            import json as _json
            cmd_file = "/tmp/inkyberry_cmd.json"
            cmd = {}
            try:
                if os.path.exists(cmd_file):
                    with open(cmd_file) as f:
                        cmd = _json.load(f)
                    os.remove(cmd_file)
            except Exception:
                pass

            if cmd.get("action") == "switch" and "plugin" in cmd:
                target = cmd["plugin"]
                for i, p in enumerate(self.plugins):
                    if p.name.lower().replace(" ", "_") == target.lower().replace(" ", "_"):
                        self.current_plugin_index = i
                        logger.info(f"SIGUSR1 — switching to plugin: {p.name}")
                        break
                else:
                    logger.warning(f"SIGUSR1 switch: plugin '{target}' not found")

            elif cmd.get("action") == "prev":
                self.current_plugin_index = (self.current_plugin_index - 1) % len(self.plugins)
                logger.info(f"SIGUSR1 — prev plugin: {self.plugins[self.current_plugin_index].name}")

            elif cmd.get("action") == "next":
                self.current_plugin_index = (self.current_plugin_index + 1) % len(self.plugins)
                logger.info(f"SIGUSR1 — next plugin: {self.plugins[self.current_plugin_index].name}")

            logger.info("SIGUSR1 — refreshing display")
            t = threading.Thread(target=self._refresh_and_render, daemon=True)
            t.start()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGUSR1, on_refresh_signal)

        # Initial render
        logger.info(f"Active plugin: {self.plugins[self.current_plugin_index].name}")
        self._refresh_and_render()

        # Start schedulers
        self._schedule_rotation()
        self._schedule_refreshes()

        # Keep alive
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

        # Cleanup
        logger.info("Cleaning up...")
        self.button_handler.cleanup()
        for plugin in self.plugins:
            try:
                plugin.cleanup()
            except Exception:
                pass
        logger.info("InkyBerry stopped.")


if __name__ == "__main__":
    app = InkyBerry()

    if "--screenshot" in sys.argv:
        # Optionally specify plugin: --screenshot weather
        target_plugin = None
        ss_idx = sys.argv.index("--screenshot")
        if ss_idx + 1 < len(sys.argv) and not sys.argv[ss_idx + 1].startswith("--"):
            target_plugin = sys.argv[ss_idx + 1].lower()

        if app.plugins:
            # Find the requested plugin or use the first one
            plugin = app.plugins[app.current_plugin_index]
            if target_plugin:
                for p in app.plugins:
                    if p.name.lower().replace(" ", "_") == target_plugin or \
                       p.name.lower() == target_plugin:
                        plugin = p
                        break

            logger.info(f"Taking screenshot of: {plugin.name}")
            plugin.update_data()
            img = plugin.render()
            if img:
                # Convert palette image to RGB for saving as PNG
                if img.mode == "P":
                    # Map the 7-color palette to RGB for preview
                    palette_rgb = {
                        0: (0, 0, 0),        # BLACK
                        1: (255, 255, 255),   # WHITE
                        2: (0, 128, 0),       # GREEN
                        3: (0, 0, 255),       # BLUE
                        4: (255, 0, 0),       # RED
                        5: (255, 255, 0),     # YELLOW
                        6: (255, 165, 0),     # ORANGE
                        7: (200, 200, 200),   # CLEAN
                    }
                    from PIL import Image
                    rgb_img = Image.new("RGB", img.size, (255, 255, 255))
                    pixels = img.load()
                    rgb_pixels = rgb_img.load()
                    for py in range(img.height):
                        for px in range(img.width):
                            idx = pixels[px, py]
                            rgb_pixels[px, py] = palette_rgb.get(idx, (128, 128, 128))
                    rgb_img.save("/home/pi/inkyberry/screenshot.png")
                else:
                    img.save("/home/pi/inkyberry/screenshot.png")
                logger.info("Screenshot saved to ~/inkyberry/screenshot.png")
        sys.exit(0)

    app.run()
