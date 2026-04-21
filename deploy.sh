#!/usr/bin/env bash
set -euo pipefail

APP_NAME="auctionninja_local_app"
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
  ./ "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"

ssh "${REMOTE_USER}@${REMOTE_HOST}" <<EOF
set -euo pipefail

cd "${REMOTE_DIR}"

python3 -m venv venv
source venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

systemctl restart ${SERVICE_NAME}
systemctl status ${SERVICE_NAME} --no-pager
EOF

echo "Deploy complete."