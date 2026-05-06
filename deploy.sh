#!/usr/bin/env bash
set -euo pipefail

if [[ -n $(git status -s) ]]; then
  echo "Error: You have uncommitted changes. Please commit or stash them before deploying."
  exit 1
fi

APP_NAME="inventory_manager"
REMOTE_HOST="162.243.127.166"
REMOTE_USER="tobryan"
REMOTE_DIR="/opt/${APP_NAME}"
SERVICE_NAME="${APP_NAME}"

echo "Deploying to ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}"

rsync -avz --delete \
  --exclude 'venv/' \
  --exclude '.env' \
  --exclude '__pycache__/' \
  --exclude '.git/' \
  --exclude 'data/' \
  --exclude '.pytest_cache/' \
  --exclude 'tests/' \
  --exclude '.DS_Store' \
  ./ "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"

ssh "${REMOTE_USER}@${REMOTE_HOST}" <<EOF
set -euo pipefail

cd "${REMOTE_DIR}"

if [ ! -f ".env" ]; then
    echo "WARNING: .env file is missing on the server! The app may fail to start."
fi

mkdir -p data
python3 -m venv venv
source venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

# Ensure the app can write to its socket and data directory
sudo chown -R tobryan:www-data .
sudo chmod -R 775 data

sudo systemctl restart ${SERVICE_NAME}
sudo systemctl status ${SERVICE_NAME} --no-pager
EOF

echo "Deploy complete."