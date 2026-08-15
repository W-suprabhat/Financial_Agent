# Financial Extraction Agent

Extracts structured financial data from PDF documents — income statements, balance
sheets, cash flow statements, 10-Ks, annual reports — and returns JSON, CSV, or Excel.

Built as a **LangGraph agent** on **Azure OpenAI**, with a single-page web UI and
Azure Blob Storage for source documents and generated files.

---

## How it works

```
Browser (static/index.html)
    │  one PDF per request
    ▼
FastAPI  (app/main.py)
    │
    ├─► Azure Blob ── source PDF archived to  financial-agent/uploads/
    │
    ▼
LangGraph agent  (app/agent.py)

    START ─► load_document ─► extract_financial_data ─► validate_data
                                      ▲                      │
                                      └──── retry ───────────┤
                                       (confidence < 0.6,     │
                                        max 3 attempts)       ▼
                                                        format_output ─► END
    │
    ▼
Azure OpenAI  (app/extractor.py)
    PDF sent natively via the Responses API `input_file`.
    Azure extracts the text layer AND a rendered image of each page, so this
    also works on scanned documents with no text layer.
    │
    ▼
Formatters (app/formatters.py) ─► JSON / CSV / Excel
    │
    └─► Azure Blob ── output archived to  financial-agent/outputs/{json,csv,excel}/
```

The retry edge is what makes this an agent rather than a single API call: it reads the
completeness of its own output and re-runs extraction with sharper instructions that
reference the previous attempt.

---

## Layout

```
app/
  config.py       All environment configuration, read lazily in one place
  models.py       FinancialData dataclass + invariants (signs, ratios, confidence)
  extractor.py    Azure OpenAI call and prompt
  agent.py        LangGraph graph (nodes, edges, retry logic)
  formatters.py   JSON / CSV / Excel output
  storage.py      Azure Blob Storage
  main.py         FastAPI app and routes
static/
  index.html      Single-page UI (no build step, no npm)
tests/
  fixtures/       Synthetic test PDFs (regenerate with scripts/make_fixtures.py)
  test_models.py  Sign normalization, ratios, confidence, serialization
  test_formatters.py
  test_api.py     Routes and error handling, with agent + blob stubbed
scripts/
  extract.py            CLI: extract from a PDF on disk
  make_fixtures.py      Regenerate the test PDFs
  benchmark_models.py   Score Azure deployments on known-correct values
docs/archive/     Superseded planning notes — see the warning below
```

---

## Setup

```bash
pip install -r requirements.txt          # runtime
pip install -r requirements-dev.txt      # + tests, fixtures, LangGraph Studio

cp .env.example .env                     # then fill in your values
```

### Required environment variables

| Variable | Notes |
|---|---|
| `AZURE_OPENAI_BASE_URL` | Must end in `/openai/v1/` |
| `AZURE_OPENAI_API_KEY` | |
| `AZURE_OPENAI_DEPLOYMENT` | **Deployment** name, not model name — see below |
| `DOC_PARSER_BLOB_CONNECTION_STRING` | Optional; extraction works without it, archiving does not |
| `DOC_PARSER_BLOB_CONTAINER_NAME` | |

Real environment variables take precedence over `.env`, which is what Vercel and
Azure App Service need.

---

## Run

```bash
# Web UI + API
uvicorn app.main:app --reload --port 8000
# → http://localhost:8000        UI
# → http://localhost:8000/docs   Swagger

# One-off from the command line
python -m scripts.extract path/to/statement.pdf
python -m scripts.extract path/to/statement.pdf --format csv --out out.csv

# Tests (no API calls, no token spend)
pytest

# Visual graph + step debugger
langgraph dev
# → https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024
```

---

## API

| Endpoint | Purpose |
|---|---|
| `GET /` | Web UI |
| `POST /extract?output_format=json\|csv\|excel` | Extract one PDF. `json` returns data + `job_id` |
| `POST /extract-batch?output_format=…` | Several PDFs in one request (see timeout note) |
| `POST /api/export` | `{"job_ids": [...], "format": "excel"}` — build a file from finished jobs |
| `GET /api/outputs` | List generated files in blob storage |
| `GET /api/download?blob=…` | Stream a generated file back |
| `GET /health` | Health + configuration check |
| `GET /api/info` | Agent configuration and endpoint list |

```bash
curl -X POST "http://localhost:8000/extract?output_format=json" \
  -F "file=@statement.pdf"
```

`/extract-batch` processes files sequentially, so a large batch in one request can
exceed a serverless timeout. The UI sends **one file per request** to avoid this and
to show real per-file progress.

---

## Choosing a model deployment

Azure **deployment names do not have to match the model** they serve, and a model
asked to identify itself will often answer incorrectly. Read the real mapping from
Azure AI Foundry → Deployments. In this project's own resource, a deployment named
`DIR_GPT4O` actually served `gpt-5.1`.

Benchmarked on the fixture PDFs, **every** deployment tested scored 100% on both
documents — extraction here is transcription, not reasoning, so accuracy did not
differentiate them. Latency and cost did:

| Deployment | Real model | Text PDF | Scanned PDF |
|---|---|---|---|
| `gpt-4o_latest` | gpt-4o | 23/23 · 5.3s | 8/8 · 9.0s |
| `DIR_ChatBot` | gpt-4.1-mini | 23/23 · 10.8s | 8/8 · 7.7s |
| `DIR_GPT4O` | gpt-5.1 | 23/23 · 14.3s | 8/8 · 11.9s |
| `DIR_GPT5` | gpt-5 | 23/23 · 22.5s | 8/8 · 14.2s |
| `Genie-Team-GPT-5.4` | gpt-5.4 | 23/23 · 15.8s | 8/8 · 43.6s |

**Prefer a non-reasoning model.** Reasoning models cost more, take 2–3× longer, and
add nothing when the numbers are printed on the page. They can also spend their whole
token budget on reasoning and return an empty response, which `extractor.py` detects
and reports explicitly.

Re-run this yourself:

```bash
python -m scripts.benchmark_models gpt-4o_latest DIR_ChatBot
```

⚠️ These fixtures are **synthetic single-page** documents. Validate against real
multi-page filings before committing to a model — that is where models will actually
differentiate.

---

## Two behaviours worth understanding

**`extraction_confidence` measures completeness, not correctness.** It is the fraction
of the fields relevant to the detected `document_type` that came back populated. A
balance-sheet-only document legitimately scores low on income statement fields. Don't
read a low score as "wrong" without looking at the document.

**Signs are normalized.** Statements print costs and liabilities in parentheses, and
the model reproduces that inconsistently. Costs, expenses, liabilities, debt, and
assets are stored as positive magnitudes. Sign is preserved only where a negative is
meaningful: `gross_profit`, `operating_income`, `net_income`, `total_equity`, and the
four cash flow fields. Without this, a negative `current_liabilities` silently flips
`current_ratio` negative.

---

## Deploy

`vercel.json` is configured for Vercel with `app/main.py` as the entrypoint and
`static/**` bundled. Set each environment variable as a Vercel secret.

Vercel's function duration limit (300s on current plans) comfortably covers
single-document extraction at 5–15s. If you move to batch processing of large
filings, prefer a host without a request timeout — Azure Container Apps or App
Service, which also keeps the workload next to the Azure OpenAI resource.

---

## docs/archive

⚠️ **`docs/archive/` contains superseded planning notes with code that does not
work.** They document how the project was originally scoped and are kept for
history only. Known errors in them: Anthropic `client.messages.create` syntax
labelled as OpenAI, a `"type": "document"` content block that does not exist in the
OpenAI SDK, `openpyxl==3.11.0` (never released), and a PDF-to-image conversion step
that native file input makes unnecessary. **Trust this README instead.**
