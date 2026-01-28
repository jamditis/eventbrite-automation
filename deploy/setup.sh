#!/bin/bash
# Eventbrite Automation - Raspberry Pi Setup Script
# Run this script on the Pi after cloning the repository
#
# Usage:
#   cd /home/pi/eventbrite-automation
#   chmod +x deploy/setup.sh
#   ./deploy/setup.sh

set -e  # Exit on error

echo "=================================================="
echo "  Eventbrite Automation - Raspberry Pi Setup"
echo "=================================================="

# Check we're in the right directory
if [ ! -f "webhook_server.py" ]; then
    echo "Error: webhook_server.py not found."
    echo "Please run this script from the project root directory."
    echo "  cd /home/pi/eventbrite-automation && ./deploy/setup.sh"
    exit 1
fi

PROJECT_DIR=$(pwd)
echo "Project directory: $PROJECT_DIR"

# Step 1: Create virtual environment
echo ""
echo "[1/6] Creating Python virtual environment..."
if [ -d "venv" ]; then
    echo "  Virtual environment already exists"
else
    python3 -m venv venv
    echo "  Created venv/"
fi

# Step 2: Install dependencies
echo ""
echo "[2/6] Installing Python dependencies..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
echo "  Dependencies installed"

# Step 3: Check for .env file
echo ""
echo "[3/6] Checking environment configuration..."
if [ ! -f ".env" ]; then
    echo "  Warning: .env file not found!"
    echo ""
    echo "  Please create .env with the following variables:"
    echo "    AIRTABLE_PAT=your_airtable_pat"
    echo "    AIRTABLE_BASE_ID=your_base_id"
    echo "    AIRTABLE_TABLE_ID=your_table_id"
    echo "    GEMINI_API_KEY=your_gemini_key"
    echo "    EVENTBRITE_PRIVATE_TOKEN=your_eventbrite_token"
    echo "    WEBHOOK_SECRET=your_optional_secret"
    echo ""
    echo "  You can create it with: nano .env"
    ENV_MISSING=true
else
    echo "  .env file found"
    ENV_MISSING=false
fi

# Step 4: Create log directory
echo ""
echo "[4/6] Creating log directory..."
if [ ! -d "/var/log/eventbrite-automation" ]; then
    sudo mkdir -p /var/log/eventbrite-automation
    sudo chown $USER:$USER /var/log/eventbrite-automation
    echo "  Created /var/log/eventbrite-automation"
else
    echo "  Log directory already exists"
fi

# Step 5: Install systemd service
echo ""
echo "[5/6] Installing systemd service..."
SERVICE_FILE="deploy/eventbrite-automation.service"

# Update the service file with the correct paths if needed
CURRENT_USER=$(whoami)
if [ "$CURRENT_USER" != "pi" ]; then
    echo "  Updating service file for user: $CURRENT_USER"
    sed -i "s|User=pi|User=$CURRENT_USER|g" $SERVICE_FILE
    sed -i "s|Group=pi|Group=$CURRENT_USER|g" $SERVICE_FILE
    sed -i "s|/home/pi/|/home/$CURRENT_USER/|g" $SERVICE_FILE
fi

sudo cp $SERVICE_FILE /etc/systemd/system/
sudo systemctl daemon-reload
echo "  Service installed"

# Step 6: Test the server (if .env exists)
echo ""
echo "[6/6] Testing webhook server..."
if [ "$ENV_MISSING" = false ]; then
    # Quick test of the configuration
    source venv/bin/activate
    python -c "from config import validate_config; validate_config(); print('  Configuration is valid')" 2>/dev/null || {
        echo "  Warning: Configuration validation failed"
        echo "  Check your .env file for missing or incorrect values"
    }
else
    echo "  Skipped - .env file missing"
fi

echo ""
echo "=================================================="
echo "  Setup Complete!"
echo "=================================================="
echo ""
echo "Next steps:"
echo ""
if [ "$ENV_MISSING" = true ]; then
    echo "1. Create the .env file:"
    echo "   nano .env"
    echo ""
fi
echo "2. Start the service:"
echo "   sudo systemctl start eventbrite-automation"
echo ""
echo "3. Enable auto-start on boot:"
echo "   sudo systemctl enable eventbrite-automation"
echo ""
echo "4. Check service status:"
echo "   sudo systemctl status eventbrite-automation"
echo ""
echo "5. View logs:"
echo "   tail -f /var/log/eventbrite-automation/webhook.log"
echo ""
echo "6. Test the endpoint:"
echo "   curl http://localhost:5000/"
echo ""
echo "7. Set up external access (ngrok or Cloudflare tunnel)"
echo "   See: deploy/README.md"
echo ""
