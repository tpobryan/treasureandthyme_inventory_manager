# Auction CSV Helper

A simple local web app for your AuctionNinja workflow.

What it does:
- lets you upload item photos from desktop or iPhone/iPad photo library
- sends the photos to the OpenAI API for a draft title, description, tags, and category
- shows the draft in a web form so you can edit it manually
- saves the final row into a local CSV file with the next sequential lot number
- leaves the rest of the CSV columns available for editing before save

## Files created by the app
- `data/auction_items.csv`
- `data/lot_state.json`
- `data/auction_photo_state.json` when FTP uploads are enabled
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
```

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

## Run tests

```bash
python3 -m pytest
```

## Notes
- The final lot number is assigned when you click **Save to CSV**.
- The next lot preview is shown before save.
- Lot numbers and FTP photo indexes are stored in local JSON state files under `data/`.
- If you run the app for multiple users at once, treat it as a small local tool rather than a concurrency-safe multi-user system.
- The built-in Flask server is fine on a trusted home network, but Flask's docs recommend a production WSGI server such as Waitress for anything beyond development.
