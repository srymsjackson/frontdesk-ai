# AI Reception

An AI phone receptionist for small businesses (barbershops, salons, etc.) that answers every inbound call, has a natural back-and-forth conversation to collect booking details, and hands the owner a ready-to-follow-up lead — no missed calls, no voicemail.

Built solo end-to-end: telephony integration, conversational state machine, multi-tenant data model, and a live real-time demo page.

## What it does

1. A customer calls the business's Twilio number.
2. The AI receptionist answers and asks what they need.
3. Over a few natural turns, it extracts the caller's name, requested service, and preferred time (plus optional fields like staff preference) — asking one clarifying question at a time instead of running a rigid script.
4. Once it has what it needs, it saves the lead, notifies the owner with a summary and (optionally) texts the customer a booking link, and ends the call politely.
5. The lead shows up instantly on a live dashboard / demo feed via WebSocket.

## Why it's interesting from an engineering standpoint

- **Conversational state machine over telephony.** Twilio's `Gather`/webhook model is stateless per request; the app tracks per-call progress (`app/routes/voice.py`) across turns, merges newly-extracted fields into what's already known, and decides whether to ask another question or complete the call.
- **LLM extraction with a real fallback path.** `app/services/ai_service.py` prompts OpenAI to return structured JSON (fields + a natural-sounding follow-up question) every turn. If the API call fails or returns malformed JSON, regex-based extractors kick in so a booking is never silently lost to an upstream error.
- **Config-driven, multi-tenant conversation flow.** `Business` / `BusinessConfig` models let each business customize its greeting, required fields, prompts, services, and staff list from the database — adding a new business doesn't require touching conversation logic.
- **Real-time visibility.** A lightweight WebSocket broadcast manager (`app/websocket_manager.py`) pushes `call_started` / `new_lead` / `call_ended` events to any connected client, powering both a live public demo page and an internal dashboard, with PII masked on the public feed.
- **Graceful degradation on voice.** Optional ElevenLabs TTS for a more natural voice, with automatic fallback to Twilio's built-in `<Say>` if generation fails or the feature is disabled.

## Performance

Voice conversations live or die on latency — a caller will tolerate maybe a second of silence before the call feels broken. The design leans toward keeping each turn fast:

- **Right-sized model.** Extraction runs on `gpt-5-mini` (see `app/config.py`) rather than a frontier model. Pulling a few structured fields and drafting a short follow-up question is a narrow task that doesn't need heavyweight reasoning, so the smaller, faster model is the deliberate choice here.
- **Prompt slimming.** The system prompt is trimmed to the minimum needed for reliable field extraction, reducing input tokens per turn.
- **TTS caching.** Repeated phrases (greetings, confirmations) are cached in memory by `voice_service.py`, so ElevenLabs generation only happens once per unique line.

Formal end-to-end latency benchmarking (from end of caller speech to AI reply) is a tracked next step rather than a measured claim today.

## Testing

The booking flow is covered by a `pytest` suite that runs **without hitting the OpenAI API**: the deterministic regex/cleaning layer is tested directly, and `analyze_customer_turn` is tested with the OpenAI client monkeypatched to simulate both hard failures and malformed responses. This keeps the suite fast, free, and reproducible — the numbers below come from the same code that runs in production. Results are written to `tests/results.md` on every run.

```bash
pip install -r requirements.txt
pytest                 # deterministic suite, no API key needed
pytest -m live         # optional: also runs live-API integration tests
```

**Latest run: 44/44 deterministic scenarios passing**, across four categories:

| Category | Passed | Total |
|---|---|---|
| Conversation edge cases (interruptions, mumbled/unusable input, mid-call changes) | 12 | 12 |
| Ambiguous bookings (vague times, multiple services, missing fields) | 14 | 14 |
| Hostile / off-script callers (off-topic questions, profanity, prompt-pulling) | 9 | 9 |
| Failure injection (OpenAI timeouts, malformed JSON → regex fallback path) | 9 | 9 |

Two additional live-API integration tests (`-m live`) verify real end-to-end extraction; they're skipped by default so the suite needs no key or spend to run.

The suite earned its keep immediately: on its first run it caught a real bug in the regex fallback, where a hostile caller saying "this is stupid" was being parsed as the caller name "Stupid." The fix — a conservative stop-list that biases toward missing a name rather than inventing one — is locked in by regression tests.

## Tech stack

| Layer | Choice |
|---|---|
| API framework | FastAPI (Python) |
| ORM / models | SQLModel (SQLAlchemy) |
| Telephony | Twilio Programmable Voice + SMS, TwiML |
| AI extraction | OpenAI `gpt-5-mini` (structured JSON responses) |
| Text-to-speech | ElevenLabs (optional, feature-flagged) |
| Real-time updates | Native FastAPI WebSockets |
| Database | SQLite (local) / PostgreSQL (prod) |
| Deployment | Railway |

## Architecture

```
Caller dials Twilio number
        │
        ▼
POST /voice/incoming ──► resolve Business by number ──► TwiML <Gather> prompt
        │
        ▼
POST /voice/collect  (repeats each turn)
        │  ├─ analyze_customer_turn() → OpenAI extracts fields + drafts next question
        │  ├─ merge extracted fields into in-memory call state
        │  └─ missing required field? ──► ask again  |  else ──► continue
        ▼
Lead persisted (SQLModel) ──► owner summary + optional customer SMS ──► WebSocket broadcast
                                                                              │
                                                                              ▼
                                                                Live demo / dashboard feed
```

## Project structure

```
app/
  main.py                    FastAPI app setup, router wiring, startup hook
  config.py                  Environment-driven settings (Pydantic)
  db.py                      SQLModel engine/session
  models.py                  Business, Lead, BusinessConfig
  schemas.py                 API/service payload schemas
  websocket_manager.py       In-memory pub/sub for live event broadcast
  routes/
    voice.py                 Twilio voice webhooks (incoming call, multi-turn collect)
    sms.py, calls.py         SMS + REST call-flow endpoints
    leads.py                 Lead listing endpoints
    dashboard.py             Key-protected HTML dashboard for viewing leads
    demo.py                  Public live demo page + WebSocket feed
  services/
    ai_service.py            OpenAI extraction + regex fallback parsing
    voice_service.py         ElevenLabs TTS with in-memory audio cache
    twilio_service.py        Outbound SMS helper
    lead_service.py          Lead persistence helpers
    config_service.py        Per-business prompt/required-field resolution
tests/                       pytest suite (see Testing) + auto-generated results.md
seed.py                      Seeds a sample business for local dev
```

## Running locally

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt

cp .env.example .env          # fill in Twilio / OpenAI (/ ElevenLabs) keys
python seed.py                # creates a sample business row
uvicorn app.main:app --reload
```

To receive real calls locally, expose the app with a tunnel (e.g. `ngrok http 8000`) and point the Twilio number's voice webhook at `<tunnel-url>/voice/incoming`.

Visit `/demo` for the public live-activity page, or `/dashboard/leads?key=<DASHBOARD_KEY>` for the internal lead list.

## Known limitations / next steps

- **Call state is in-process memory.** `CALL_STATE` in `voice.py` is fine for a single Railway instance but wouldn't survive a restart mid-call or horizontal scaling — a Redis-backed store is the natural next step, noted directly in the code.
- **Outbound SMS is stubbed in the demo build.** `twilio_service.send_sms` is fully implemented but commented out to avoid sending real texts from a portfolio deployment; re-enabling it requires an upgraded Twilio account (and A2P 10DLC registration for US traffic).
- **Dashboard auth is demo-grade.** The `?key=` query-string check is intentionally minimal for a portfolio demo; a production deployment would move to session-based auth (a key in a URL gets logged by servers, proxies, and browser history).
- **Latency isn't formally benchmarked yet** (see Performance).