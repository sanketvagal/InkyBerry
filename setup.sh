#!/bin/bash
# InkyBerry Setup Script
# Run this on your Raspberry Pi after a fresh OS install

set -e

echo "=============================="
echo "  InkyBerry Setup"
echo "=============================="

# Update system
echo "[1/6] Updating system packages..."
sudo apt update && sudo apt upgrade -y

# Install system dependencies
echo "[2/6] Installing system dependencies..."
sudo apt install -y python3-pip python3-venv python3-dev \
    libopenjp2-7 libtiff6 libopenblas-dev \
    git fonts-dejavu-core


# Configure SPI for Inky Impression
echo "[2b/6] Configuring SPI overlay for Inky Impression..."
CONFIG_FILE="/boot/firmware/config.txt"
if [ ! -f "$CONFIG_FILE" ]; then
    CONFIG_FILE="/boot/config.txt"  # fallback for older Pi OS
fi

if grep -q "dtoverlay=spi0-0cs" "$CONFIG_FILE"; then
    echo "  spi0-0cs overlay already set, skipping."
else
    echo "dtoverlay=spi0-0cs" | sudo tee -a "$CONFIG_FILE" > /dev/null
    echo "  Added dtoverlay=spi0-0cs to $CONFIG_FILE"
    NEEDS_REBOOT=1
fi

# Clone and install inky library
echo "[3/6] Installing Pimoroni Inky library..."
if [ ! -d "$HOME/inky" ]; then
    cd $HOME
    git clone https://github.com/pimoroni/inky
    cd inky
    # Use the install script which handles venv + dependencies
    ./install.sh <<< "y"
else
    echo "Inky library already cloned, skipping..."
fi

# Create our virtual environment (or use pimoroni's if it exists)
echo "[4/6] Setting up Python environment..."
VENV_PATH="$HOME/.virtualenvs/inkyberry"
if [ -d "$HOME/.virtualenvs/pimoroni" ]; then
    echo "Found pimoroni venv from inky installer, using that..."
    VENV_PATH="$HOME/.virtualenvs/pimoroni"
fi

if [ ! -d "$VENV_PATH" ]; then
    python3 -m venv --system-site-packages "$VENV_PATH"
fi

source "$VENV_PATH/bin/activate"

# Install Python dependencies
echo "[5/6] Installing Python packages..."
pip install --upgrade pip
pip install yfinance requests pyyaml pillow RPi.GPIO gpiozero gpiodevice flask

# Download a nice readable font
echo "[6/6] Downloading fonts..."
FONT_DIR="$HOME/inkyberry/fonts"
mkdir -p "$FONT_DIR"
if [ ! -f "$FONT_DIR/DejaVuSans-Bold.ttf" ]; then
    cp /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf "$FONT_DIR/" 2>/dev/null || true
    cp /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf "$FONT_DIR/" 2>/dev/null || true
fi

# Create photos directory for photo frame plugin
mkdir -p "$HOME/inkyberry/photos"

# Create systemd service
echo "Creating systemd service..."
sudo tee /etc/systemd/system/inkyberry.service > /dev/null << EOF
[Unit]
Description=InkyBerry E-Ink Display Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME/inkyberry
Environment="PATH=$VENV_PATH/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=$VENV_PATH/bin/python main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable inkyberry.service

# Create web dashboard systemd service
echo "Creating web dashboard service..."
sudo tee /etc/systemd/system/inkyberry-web.service > /dev/null << EOF
[Unit]
Description=InkyBerry Web Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME/inkyberry
Environment="PATH=$VENV_PATH/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=$VENV_PATH/bin/python web/server.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable inkyberry-web.service

# Allow the web dashboard to control the inkyberry service and reboot
echo "Configuring sudoers for service control..."
sudo tee /etc/sudoers.d/inkyberry > /dev/null << EOF
$USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start inkyberry.service
$USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop inkyberry.service
$USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart inkyberry.service
$USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start inkyberry-web.service
$USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop inkyberry-web.service
$USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart inkyberry-web.service
$USER ALL=(ALL) NOPASSWD: /usr/sbin/reboot
$USER ALL=(ALL) NOPASSWD: /usr/sbin/shutdown
EOF
sudo chmod 0440 /etc/sudoers.d/inkyberry

echo ""
echo "=============================="
echo "  Setup Complete!"
echo "=============================="
echo ""
echo "Virtual env: $VENV_PATH"
echo "Activate with: source $VENV_PATH/bin/activate"
echo ""
echo "To start InkyBerry:"
echo "  sudo systemctl start inkyberry"
echo ""
echo "To start the Web Dashboard:"
echo "  sudo systemctl start inkyberry-web"
echo "  Then open http://inkyberry.local:5000 in your browser"
echo ""
echo "To view logs:"
echo "  journalctl -u inkyberry -f"
echo ""
echo "To run manually:"
echo "  source $VENV_PATH/bin/activate"
echo "  cd ~/inkyberry && python main.py"
echo "  # In another terminal:"
echo "  cd ~/inkyberry && python web/server.py"
echo ""
if [ "${NEEDS_REBOOT}" = "1" ]; then
    echo "⚠️  REBOOT REQUIRED"
    echo "   dtoverlay=spi0-0cs was added to $CONFIG_FILE."
    echo "   Run: sudo reboot"
    echo "   Then start InkyBerry after reboot."
    echo ""
fi
