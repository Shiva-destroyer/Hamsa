# Setup

Two levels: **A. offline** — database, tests and a full simulated conversation, no Meta account needed — and **B. live WhatsApp** ([DEPLOYMENT.md](DEPLOYMENT.md)). Do A first.

Developed and tested on Linux with Python 3.14 and PostgreSQL 16. Python 3.11 or newer is expected to work but has not been tested.

## 1. PostgreSQL 16

    docker compose up -d db          # from the repository root; data persists in a Docker volume

Any PostgreSQL 16 you can reach through `DATABASE_URL` works (the database-setup script needs a role that can `CREATE DATABASE`). It has only been tested against the Docker image above.

## 2. System tools

`tesseract` is needed by the photo path. `ffmpeg` is only needed to regenerate the voice notes, and `cloudflared` only for live WhatsApp.

- Arch: `pacman -S tesseract tesseract-data-eng ffmpeg`
- Debian/Ubuntu: `apt install tesseract-ocr ffmpeg`
- macOS: `brew install tesseract ffmpeg`

To also read Hindi and Kannada text in photos, add the Tesseract language data (`tesseract-data-hin`, `tesseract-data-kan`, or `tesseract-ocr-hin`, `tesseract-ocr-kan`).

Optional Florence-2 photo-text reader (`FLORENCE=1`, the default): `pip install torch --index-url https://download.pytorch.org/whl/cpu` and `transformers`. The first start downloads about 470 MB of model weights from Hugging Face into `~/.cache/huggingface`; images are processed on your machine only. Set `FLORENCE=0` to use Tesseract only.

## 3. Python environment

    python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
    pip install -r api/requirements.txt
    cp api/.env.example api/.env                      # then edit; never commit .env

For offline work set `DRY_RUN=1` in `api/.env` (replies are printed instead of sent) and leave the `WA_*` values blank. Do not put an inline `#` comment after an *empty* value: python-dotenv would read the comment as the value.

## 4. Build the database

    python api/scripts/db_setup.py --db hamsa --reset          # schema, seed, migrations, demo fixups
    python api/scripts/db_setup.py --db hamsa --fixups-only    # re-run to reset demo state (idempotent)
    export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/hamsa
    python api/scripts/load_real_nsq.py data/real/nsq_real_loaded.csv    # optional: the 529 real CDSCO rows

Expected counts after `--reset`: 126 products, 304 aliases, 650 batches, 200 NSQ alerts, 2,528 scan events, 15 tables with Row-Level Security. Use a separate database for tests (`--db hamsa_test`) rather than the one you demo from.

The `Makefile` wraps the common steps: `make install`, `make db`, `make real-data`, `make test`, `make rehearse`, `make run`.

## 5. Tests, rehearsal, load, preflight

    pytest -q api/tests                          # full suite
    python api/scripts/rehearse.py               # plays the conversation in en/hi/kn: "ALL BEATS PASS (en/hi/kn)"
    python api/scripts/simulate_load.py          # 50 messages from 10 numbers: "LOAD OK"
    ENABLE_SCHEDULER=1 python api/scripts/preflight.py --local   # readiness checks that need neither Meta nor the tunnel

`rehearse.py`, `simulate_load.py` and the end-to-end and chaos tests force `DRY_RUN=1` and never contact Meta. Two tests load the real Florence-2 weights and run only with `TEST_FLORENCE=1`.

## 6. Sample packs

    python api/scripts/make_demo_packs.py --verify   # 10 PNGs; checks each gives its expected verdict
    python api/scripts/print_sheet.py                # api/assets/packs/PRINT_ME.pdf (A4, 2 per page, 5 pages)
    python api/scripts/make_qr_sheet.py --verify     # api/assets/packs/QR_SHEET_15.pdf (one A4 page, 15 packs)

Print at 100% on plain white paper and photograph the paper, never a screen. Expected result per pack: [api/assets/packs/README.md](../api/assets/packs/README.md).

## 7. Voice notes

The 15 voice notes are committed. Regenerate them only after editing the Safe Action text in `api/texts.py`:

    python api/scripts/gen_voice.py                  # edge-tts (gTTS fallback) -> OGG/OPUS mono
    python api/scripts/gen_voice.py --verify         # "15/15 OK"

## 8. Real NSQ data

    python api/scripts/load_real_nsq.py --help       # download/parse CDSCO PDFs, page-text guard, load as data_origin='cdsco_real'
    python api/scripts/load_real_nsq.py --pick-demo  # candidate real batches for the DEMO_REAL_BATCH setting

If the CDSCO site is unreachable, download the monthly NSQ PDFs by hand into `data/real/pdfs/` and run the same parser.

## 9. Run the API locally

    cd api && DRY_RUN=1 uvicorn app:app --port 8000
    python api/scripts/simulate_inbound.py --text hi     # simulates Meta delivering a message

Bind to localhost only: the per-IP rate limit trusts `CF-Connecting-IP`, which is only safe behind the Cloudflare tunnel.

## Environment variables

`api/.env.example` lists every variable with a placeholder. Optional: `WA_MARK_AS_READ=1` sends read receipts; `TEST_FLORENCE=1` enables the two real-weights tests; `FLORENCE_MODEL` overrides the model repository.
