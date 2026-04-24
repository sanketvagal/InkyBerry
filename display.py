"""
InkyBerry Display Module
Wraps the Inky Impression display and provides Pillow drawing helpers.
"""

import os
import logging
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("inkyberry.display")

# Inky Impression 7.3" color palette indices
# These map to the 7 colors the display can show
BLACK = 0
WHITE = 1
GREEN = 2
BLUE = 3
RED = 4
YELLOW = 5
ORANGE = 6
CLEAN = 7  # Used for clearing the display

COLOR_NAMES = {
    "black": BLACK, "white": WHITE, "green": GREEN,
    "blue": BLUE, "red": RED, "yellow": YELLOW,
    "orange": ORANGE, "clean": CLEAN,
}

# Font directory
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


class Display:
    """Manages the Inky Impression display and provides drawing utilities."""

    def __init__(self, config):
        self.config = config
        self.width = config.get("display", {}).get("width", 800)
        self.height = config.get("display", {}).get("height", 480)
        self.rotation = config.get("display", {}).get("rotation", 0)
        self.saturation = config.get("display", {}).get("saturation", 0.5)
        self.inky = None
        self._fonts = {}
        self._init_display()

    def _init_display(self):
        """Initialize the Inky display hardware."""
        try:
            from inky.auto import auto
            self.inky = auto()
            self.inky.set_border(WHITE)
            # Use actual display dimensions
            self.width = self.inky.width
            self.height = self.inky.height
            logger.info(f"Display initialized: {self.width}x{self.height}")
        except Exception as e:
            logger.warning(f"Could not init display hardware: {e}")
            logger.warning("Running in headless/preview mode")
            self.inky = None

    def get_font(self, size=20, bold=False):
        """Load a DejaVu font at the given size. Cached."""
        key = (size, bold)
        if key not in self._fonts:
            font_name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
            font_path = os.path.join(FONT_DIR, font_name)
            if not os.path.exists(font_path):
                # Fallback to system fonts
                font_path = f"/usr/share/fonts/truetype/dejavu/{font_name}"
            if not os.path.exists(font_path):
                logger.warning(f"Font not found: {font_path}, using default")
                self._fonts[key] = ImageFont.load_default()
            else:
                self._fonts[key] = ImageFont.truetype(font_path, size)
        return self._fonts[key]

    def create_canvas(self, bg_color=WHITE):
        """Create a fresh PIL Image canvas for drawing."""
        img = Image.new("P", (self.width, self.height), bg_color)
        draw = ImageDraw.Draw(img)
        return img, draw

    def show(self, image):
        """Push an image to the Inky display."""
        # Always save a preview for the web dashboard
        self._save_preview(image)

        if self.inky is None:
            logger.info(f"Preview saved to {self._preview_path}")
            return

        self.inky.set_image(image, saturation=self.saturation)
        self.inky.show()
        logger.info("Display updated")

    def _save_preview(self, image):
        """Save an RGB preview PNG for the web dashboard."""
        preview_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "preview.png"
        )
        self._preview_path = preview_path
        try:
            if image.mode == "P":
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
                rgb_img = Image.new("RGB", image.size, (255, 255, 255))
                pixels = image.load()
                rgb_pixels = rgb_img.load()
                for py in range(image.height):
                    for px in range(image.width):
                        idx = pixels[px, py]
                        rgb_pixels[px, py] = palette_rgb.get(idx, (128, 128, 128))
                rgb_img.save(preview_path)
            else:
                image.save(preview_path)
        except Exception as e:
            logger.warning(f"Failed to save preview: {e}")

    def clear(self):
        """Clear the display."""
        img = Image.new("P", (self.width, self.height), CLEAN)
        self.show(img)

    # ── Drawing Helpers ──

    def draw_text_block(self, draw, text, x, y, font_size=20, bold=False,
                        color=BLACK, max_width=None, line_spacing=4):
        """Draw text with word wrapping. Returns the height used."""
        font = self.get_font(font_size, bold)
        if max_width is None:
            max_width = self.width - x - 10

        lines = self._wrap_text(text, font, max_width)
        total_height = 0
        for line in lines:
            draw.text((x, y + total_height), line, fill=color, font=font)
            bbox = font.getbbox(line)
            total_height += (bbox[3] - bbox[1]) + line_spacing
        return total_height

    def fill_dithered_grey(self, img, x, y, w, h, density=3):
        """Fill a rectangle with a dithered grey pattern.
        density=2: 50% grey (dark), 3: 33% grey (medium), 4: 25% (light).
        """
        pixels = img.load()
        for py in range(y, min(y + h, img.height)):
            for px in range(x, min(x + w, img.width)):
                if (px + py) % density == 0:
                    pixels[px, py] = BLACK
                else:
                    pixels[px, py] = WHITE

    def draw_header(self, draw, title, subtitle=None, bg_color=BLACK,
                    text_color=WHITE, compact=False, img=None):
        """Draw a header bar at the top of the display.
        compact=True gives a TRMNL-style thin bar with dithered grey bg.
        Pass img= for dithered grey fill (needs pixel access).
        """
        if compact:
            header_h = 30
            if img is not None:
                # Dithered grey background
                self.fill_dithered_grey(img, 0, 0, self.width, header_h)
                text_color = BLACK
            else:
                draw.rectangle([0, 0, self.width, header_h], fill=bg_color)
            font = self.get_font(16, bold=True)
            draw.text((10, 6), title.upper(), fill=text_color, font=font)
            if subtitle:
                sfont = self.get_font(14)
                bbox = sfont.getbbox(subtitle)
                sw = bbox[2] - bbox[0]
                draw.text((self.width - sw - 10, 8), subtitle,
                           fill=text_color, font=sfont)
            # Thin line under header
            draw.rectangle([0, header_h, self.width, header_h + 1], fill=BLACK)
            return header_h + 1
        else:
            header_h = 50
            if img is not None:
                self.fill_dithered_grey(img, 0, 0, self.width, header_h)
                text_color = BLACK
            else:
                draw.rectangle([0, 0, self.width, header_h], fill=bg_color)
            font = self.get_font(28, bold=True)
            draw.text((15, 10), title, fill=text_color, font=font)
            if subtitle:
                sfont = self.get_font(16)
                bbox = sfont.getbbox(subtitle)
                sw = bbox[2] - bbox[0]
                draw.text((self.width - sw - 15, 18), subtitle,
                           fill=text_color, font=sfont)
            return header_h

    def draw_divider(self, draw, y, color=BLACK, thickness=1):
        """Draw a horizontal divider line."""
        draw.rectangle([10, y, self.width - 10, y + thickness], fill=color)

    def draw_info_overlay(self, draw, plugin_name, plugin_index, total_plugins,
                          last_update=None, img=None):
        """Draw a small info bar at the bottom."""
        bar_h = 25
        y = self.height - bar_h
        if img is not None:
            self.fill_dithered_grey(img, 0, y, self.width, bar_h)
            text_color = BLACK
        else:
            draw.rectangle([0, y, self.width, self.height], fill=BLACK)
            text_color = WHITE
        # Thin line on top
        draw.rectangle([0, y, self.width, y + 1], fill=BLACK)
        font = self.get_font(14)
        info = f"{plugin_name}  [{plugin_index + 1}/{total_plugins}]"
        if last_update:
            info += f"  |  Updated: {last_update}"
        draw.text((10, y + 5), info, fill=text_color, font=font)

    def _wrap_text(self, text, font, max_width):
        """Word-wrap text to fit within max_width pixels."""
        words = text.split()
        lines = []
        current_line = ""
        for word in words:
            test = f"{current_line} {word}".strip()
            bbox = font.getbbox(test)
            if (bbox[2] - bbox[0]) <= max_width:
                current_line = test
            else:
                if current_line:
                    lines.append(current_line)
                current_line = word
        if current_line:
            lines.append(current_line)
        return lines or [""]
