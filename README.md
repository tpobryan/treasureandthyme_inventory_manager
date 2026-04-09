# Auction CSV Helper

A simple local web app for your AuctionNinja workflow.

What it does:
- lets you upload item photos from desktop or iPhone/iPad photo library
- sends the photos to the OpenAI API for a draft title, description, tags, and category
- shows the draft in a web form so you can edit it manually
- saves approved items with the next sequential lot number
- exports an AuctionNinja-ready CSV when you are ready to publish lots
- tracks lots by auction with statuses and export history when `DATABASE_URL` is configured

## Files created by the app
- `data/auction_items.csv`
- `data/lot_state.json`
- `data/auction_photo_state.json` when FTP uploads are enabled
- `data/exports/` for archived CSV exports
- temporary and saved image folders in `data/uploads/`

## Setup

Create and activate a virtual environment if you want:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install packages:

```bash
pip install -r requirements.txt
```

The requirements include `Pillow` and `pillow-heif` so the app can open and optimize iPhone/iPad HEIC photos before sending them to OpenAI or saving JPGs locally.

Copy the environment file and add your API key:

```bash
cp .env.example .env
```

Then edit `.env` and set:

```env
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4.1-mini
FLASK_SECRET_KEY=some-random-secret
PORT=5000
APP_LOGIN_USERNAME=admin
APP_LOGIN_PASSWORD=
```

Optional database setting if you want to store saved items outside the local CSV:

```env
DATABASE_URL=mysql://username:password@127.0.0.1:3306/auctionninja_local_app
```

If `DATABASE_URL` is set, saved items go into the database and the home page will offer a fresh CSV download for AuctionNinja import. If it is not set, the app keeps using `data/auction_items.csv` as before.

Optional login settings for hosted use:

```env
APP_LOGIN_USERNAME=admin
APP_LOGIN_PASSWORD=choose-a-strong-password
```

If `APP_LOGIN_PASSWORD` is set, the app requires login before any page or image route can be used. This is recommended before exposing the app to the internet.

Optional FTP settings for uploading saved lot photos to AuctionNinja:

```env
AUCTION_NUMBER=
FTP_HOST=
FTP_PORT=21
FTP_USERNAME=
FTP_PASSWORD=
FTP_TLS=false
```

## Run it

### Easiest local run

```bash
python app.py
```

Then open:

```text
http://127.0.0.1:5000
```

### Access from other devices on your network

This app binds to `0.0.0.0`, so other devices on your Wi‑Fi can reach it using your computer's local IP address, for example:

```text
http://192.168.1.23:5000
```

On a Mac, you can find your local IP with:

```bash
ipconfig getifaddr en0
```

If that returns nothing, try:

```bash
ipconfig getifaddr en1
```

## Recommended run command for a steadier local server

```bash
waitress-serve --host 0.0.0.0 --port 5000 app:app
```

## Auction workflow with the database enabled

When `DATABASE_URL` is configured, the app becomes auction-aware:

- auctions have incrementing numeric ids
- one auction is always the current working auction
- new lots, exports, dashboard counts, and resumable drafts are scoped to the current auction
- auctions can be marked `preparing`, `active`, or `completed`
- lots can be moved between auctions one at a time or in bulk

Current saved-item statuses:

- `ready`
- `published`
- `needs_update`
- `removed`

The CSV is no longer the source of truth in database mode. Instead:

- saved lots live in the database
- you generate a fresh CSV export per batch or per auction when needed
- the app archives those exported CSV files under `data/exports`

## VPS deployment notes

For this app, a VPS is a strong fit because:

- Waitress can run as a simple systemd service
- nginx can reverse-proxy it cleanly
- local photo folders under `data/uploads/` remain straightforward
- you can keep using your MySQL database

This repo now includes starter files for that setup:

- [`deploy/systemd/auctionninja.service.example`](/Users/tobryan/ny5and10/auctionninja_local_app/auctionninja_local_app/deploy/systemd/auctionninja.service.example)
- [`deploy/nginx/auctionninja.conf.example`](/Users/tobryan/ny5and10/auctionninja_local_app/auctionninja_local_app/deploy/nginx/auctionninja.conf.example)

Suggested VPS stack:

- Ubuntu or Debian VPS
- nginx
- Python virtualenv
- Waitress bound to `127.0.0.1:5000`
- MySQL database
- HTTPS from Let's Encrypt

High-level setup:

1. Clone the repo to a path like `/opt/auctionninja_local_app`
2. Create a virtualenv and run `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in:
   - `OPENAI_API_KEY`
   - `FLASK_SECRET_KEY`
   - `DATABASE_URL`
   - `APP_LOGIN_PASSWORD`
   - FTP settings if you use AuctionNinja photo upload
4. Copy the systemd example into `/etc/systemd/system/auctionninja.service`
5. Copy the nginx example into `/etc/nginx/sites-available/auctionninja`
6. Enable the nginx site and systemd service
7. Add HTTPS with Certbot

Example commands:

```bash
sudo systemctl daemon-reload
sudo systemctl enable auctionninja.service
sudo systemctl start auctionninja.service
sudo systemctl status auctionninja.service
```

```bash
sudo ln -s /etc/nginx/sites-available/auctionninja /etc/nginx/sites-enabled/auctionninja
sudo nginx -t
sudo systemctl reload nginx
```

The app now exposes a simple health endpoint:

```text
/healthz
```

That endpoint stays available even when login protection is enabled, which is useful for nginx, uptime checks, or a load balancer.

## Run tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest
```

## Notes
- The final lot number is assigned when you click **Save Item**.
- The next lot preview is shown before save.
- When `DATABASE_URL` is configured, saved items and lot numbering come from the database instead of the CSV file.
- Database-backed items use statuses:
  - `ready` for newly saved items
  - `published` after a full export or selected batch export
  - `needs_update` reserved for saved-item edits after publish
  - `removed` for lots you no longer want included in exports
- Exporting a selected batch from the manage page automatically marks those lots as `published` and stores the export filename/date on each item.
- When `DATABASE_URL` is configured, export history, FTP upload tracking, auction photo counters, and active draft recovery are stored in the database, while photo files still live under `data/uploads/`.
- If you run the app for multiple users at once, treat it as a small local tool rather than a concurrency-safe multi-user system.
- The built-in Flask server is fine on a trusted home network, but Flask's docs recommend a production WSGI server such as Waitress for anything beyond development.
