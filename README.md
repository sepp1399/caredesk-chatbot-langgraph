# CareDesk LangGraph

> Multi-flow conversational assistant for hospital reception, built on
> FastAPI + LangGraph and powered by Google Gemini.

## Overview

CareDesk LangGraph is a WhatsApp-style chat assistant for a hospital
booking desk. A FastAPI server routes each conversational turn to one of
five LangGraph ReAct agents:

- **`lab_booking`** — five-phase booking funnel (insurance → doctor →
  service → slot → confirmation).
- **`manage_reservations`** — list, cancel, and reschedule the
  patient's upcoming appointments.
- **`patient_registration`** — form-style enrolment for new patients.
- **`lead_creation`** — callback request when a need cannot be served
  by the bot (no slot match, off-catalog request, etc.).
- **`ivr_to_digital`** — handoff from voice/IVR to a digital channel
  via SMS or WhatsApp link.

An LLM-based orchestrator classifies intent on every turn and the router
can hand off between agents in the same response. State for each session
is kept per-agent in a LangGraph `MemorySaver`; the session→flow mapping
is persisted to disk so a `uvicorn --reload` preserves continuity.

The booking backend is a **self-contained in-memory mock**
(`app/integrations/caredesk.py`): insurances, doctors, services,
availability and full reservation CRUD are served from seed fixtures.
That means the whole project runs standalone with nothing but a Gemini
API key — no external tenant, token, or network access required.

The bot's output language is decoupled from the system prompts: every
agent prompt is loaded from disk in its native form and a language
directive (sourced from `BOT_LANG`) is appended at load time, so the
model answers in the configured language without prompt duplication.

## Tech Stack

- **Python 3.12** — runtime.
- **FastAPI ≥ 0.115** — HTTP server, SSE streaming, static frontend
  mount.
- **LangGraph** — ReAct agent runtime, checkpointing, `pre_model_hook`
  for phase-gated tool synthesis.
- **Google Gemini** via `langchain-google-genai` — `gemini-2.5-flash`
  for the flow agents and `gemini-2.5-flash-lite` for intent
  classification, both authenticated with a single API key.
- **Pydantic v2** — request/response schemas and tool argument
  validation.
- **In-memory mock backend** — booking domain served from seed fixtures
  (`app/integrations/caredesk.py`).
- **opensearch-py** — optional knowledge-base backend (in-memory demo
  set ships out of the box).
- **Langfuse (optional)** — LangChain callback integration for tracing.

## Architecture

```
                   ┌────────────────────────────────┐
                   │     FastAPI router (/chat)     │
                   │  intent classifier + dispatch  │
                   └───────────────┬────────────────┘
                                   │
         ┌────────────┬────────────┼────────────┬───────────────┐
         ▼            ▼            ▼            ▼               ▼
    lab_booking  manage_res.  patient_reg.  lead_create   ivr_to_digital
    5-phase FSM  list/cancel/  form-style    callback      phone lookup +
                 reschedule    enrolment     request       SMS handoff
                                   │
                                   ▼
                   ┌────────────────────────────────┐
                   │  CareDesk in-memory mock        │
                   │  app/integrations/caredesk.py   │
                   │  insurances · doctors · slots · │
                   │  reservations CRUD · users      │
                   └────────────────────────────────┘
```

`info` is not a separate agent: when the orchestrator classifies a
message as `info` and no flow is active, the router answers inline via
the knowledge-base tool. When a flow is active, every agent has the
knowledge-base tool in its registry for out-of-flow questions.

```
chatbot_langgraph/
├── backend/
│   └── app/
│       ├── main.py                  FastAPI entry point, lifespan, routes
│       ├── config.py                fail-fast settings loader
│       ├── i18n.py                  static-string bundles + tone suffix
│       ├── prompts.py               TSV prompt dictionary loader
│       ├── integrations/
│       │   └── caredesk.py          in-memory mock booking backend
│       ├── agents/
│       │   ├── router.py            dispatch + cross-flow handoff
│       │   ├── llms.py              Gemini chat-model singletons
│       │   ├── tool_timing.py       per-tool duration callback
│       │   ├── orchestrator/        intent classifier
│       │   ├── lab_booking/         five-phase booking agent
│       │   ├── manage_reservations/ cancel + reschedule agent
│       │   ├── patient_registration/ new-patient enrolment agent
│       │   ├── lead_creation/       callback lead agent
│       │   └── ivr_to_digital/      voice→digital handoff agent
│       └── tools/
│           ├── shared/              auth cascade, KB, transfer
│           ├── lab_booking/         insurance/doctor/service/area/slot/booking
│           ├── manage_reservations/ list/cancel/reschedule
│           ├── patient_registration/ register
│           └── ivr_to_digital/      phone_lookup, send_link
├── frontend/
│   └── index.html                   single-file chat + debug pane
└── tests/
    └── test_flow.py                 end-to-end smoke script
```

## Prerequisites

- **Python 3.12.x**
- **pip** ≥ 23
- A **Gemini API key** — get one free at
  <https://aistudio.google.com/apikey>.

## Installation

```bash
# 1. Create and activate a virtual environment.
python -m venv venv
venv\Scripts\activate                # Windows
# source venv/bin/activate           # Linux/macOS

# 2. Install the backend dependencies.
pip install -r backend/requirements.txt

# 3. Copy the env template and add your Gemini key.
copy .env.example .env               # Windows
# cp .env.example .env               # Linux/macOS
```

Then open `.env` and set `GEMINI_API_KEY`. Everything else has a working
default.

## Configuration

| Name | Description | Required | Default |
|---|---|---|---|
| `GEMINI_API_KEY` | Google Gemini API key (also accepts `GOOGLE_API_KEY`). | **yes** | — |
| `GEMINI_MODEL` | Main chat model used by every flow agent. | no | `gemini-2.5-flash` |
| `GEMINI_ROUTER_MODEL` | Smaller/faster model used by the intent classifier. | no | `gemini-2.5-flash-lite` |
| `CAREDESK_INSTANCE_ID` | Tenant slug used when formatting demo handoff links. | no | `caredesk-demo` |
| `CAREDESK_TEST_USER_ID` | User id used when no caller is authenticated. | no | `usr_demo_001` |
| `CAREDESK_MANAGE_WEBOFF` | When truthy, exposes hidden activities/doctors tagged with `weboff=true`. | no | `false` |
| `PROMPTS_TSV_PATH` | Override path for the prompt dictionary TSV. | no | repo-root `Voicebot_Chatbot_Chiavi.tsv` |
| `PROMPTS_INSTANCE` | Lookup key (instance) in the prompt TSV. | no | `caredesk-demo` |
| `PROMPTS_LANG` | Lookup key (language) in the prompt TSV. | no | `it` |
| `BOT_LANG` | Output language for the bot (`it` / `en`). | no | `it` |
| `LOG_LEVEL` | Python logging level for the `caredesk_lg` logger tree. | no | `INFO` |
| `CORS_ORIGINS` | Comma-separated list of allowed origins, or `*`. | no | `*` |
| `LANGFUSE_BASE_URL` | Langfuse instance URL (also accepts legacy `LANGFUSE_HOST`). | no | `https://cloud.langfuse.com` |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Enable tracing when both are set. | no | — |

## Running

```bash
# Development (auto-reload). On Windows you can also use start.bat.
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 5678
# Working directory: backend/

# Smoke test (hits Gemini end-to-end against the mock backend).
python tests/test_flow.py
```

Once the server is up, browse to `http://127.0.0.1:5678/` for the chat
UI and `http://127.0.0.1:5678/docs` for the OpenAPI explorer.

### Demo data

The mock backend ships with a couple of seeded patients so the
caller-aware flows work out of the box. Enter **`3331234567`** in the
frontend phone modal to be recognised as *Marco Esposito*, who already
has an upcoming appointment (handy for testing the manage / cancel /
reschedule flow). Available services include *Visita cardiologica*,
*Ecografia addominale*, *Esame del sangue* and *Risonanza magnetica*
(deferred-email booking).

## API

See [`API.md`](./API.md) for the full reference.

## Contributing

- **Branch naming** — `feat/<short-slug>`, `fix/<short-slug>`,
  `chore/<short-slug>`.
- **Commit format** — Conventional Commits
  (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`).

## License

Personal project. Licensing terms are defined by the repository owner.
