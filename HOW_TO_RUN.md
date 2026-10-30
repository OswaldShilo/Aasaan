# How to run Aasaan

## You need

- Python 3.12 (the version the project is developed and tested on)
- One Gemini key from Google AI Studio (`GEMINI_API_KEY`). Without any model key the app starts but refuses to build notebooks, and shows a banner. There is no demo mode.

## 1. Install

```bash
git clone https://github.com/OswaldShilo/Aasaan.git
cd Aasaan
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt
```

## 2. Configure

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Open `.env` and set `GEMINI_API_KEY`. Everything else is optional:

| Key | Adds |
|---|---|
| `OPENROUTER_API_KEY` | extra Gemma fallback and a second route to the decision models |
| `TYPESAFE_API_KEY` | Jev, the typed decision model (relevance, diagnosis) |
| `SARVAM_API_KEY` | Indian-language writing, speech to text, text to speech |
| `YOUTUBE_API_KEY` | video search and chapter timestamps |
| `JINA_API_KEY`, `FIRECRAWL_API_KEY` | extra ways to read web pages |
| `OPENVERSE_CLIENT_ID`, `OPENVERSE_CLIENT_SECRET` | more image search room |

Never commit `.env`. Other settings (paths, limits, timeouts) are documented inside `.env.example`.

## 3. Run

```bash
python -m uvicorn app.main:create_app --factory --app-dir backend --port 8000
```

Open <http://localhost:8000>. Data goes to `data/aasaan.db` and `data/uploads` (override with `AASAAN_DB` and `AASAAN_UPLOADS`); the folders are created on start.

Check it is healthy:

- `GET /health` is a fast liveness check, `GET /health/ready` checks the database is writable.
- `GET /api/health/models` calls each configured model once and reports `ok` or `failed` with the reason. This also runs at startup.
- `GET /api/health/tools` shows each web tool's state, budget and last error.

## 4. Test

```bash
cd backend
python -m pytest -q                # the fast default run (skips tests marked slow or live)
python -m pytest -q -m ""          # the full suite; run this before you open a pull request
```

Tests never call a real model or the real network: they use a test double (`backend/tests/fake_provider.py`) and a fake web.
Three integration tests need the real Indra OCR engine and are skipped unless it is installed (see below).

With the server running, `python scripts/acceptance.py` runs the end-to-end acceptance checks against it.

## Switches you may want

| Setting | Effect |
|---|---|
| `AASAAN_ORCHESTRATOR=on` | mirrors each build onto the board and referee (off by default; the old pipeline stays in charge) |
| `AASAAN_TRACE=on` | developer trace at `GET /api/dev/trace/{notebook_id}` (never shown to learners) |
| `AASAAN_CHAOS="model:gemma_26b=fail,tool:commons=fail"` | forces a model or tool to fail, to check fallbacks |
| `AASAAN_LOCAL_OCR_ONLY=1` | never send page images to a model provider |
| `AASAAN_SLIDES`, `AASAAN_BOOKS`, `AASAAN_PRACTICE` = `off` | turn that extra off |

## Quality baseline

`python eval/baseline.py --label mine` builds five fixed topics with the real models and writes numbers to `eval/baseline/`. It is slow and uses your API quota.

## Troubleshooting

- **"No AI model is connected" banner:** `GEMINI_API_KEY` is missing, or the server was not restarted after editing `.env`.
- **Many fallbacks in the log:** check `/api/health/models`. Every fallback is recorded with its reason in the `events` table and the server log.
- **Port 8000 busy:** pass another `--port`.
