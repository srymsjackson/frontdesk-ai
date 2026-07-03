# AI Reception

An AI phone receptionist for small businesses (barbershops, salons, etc.) that answers every inbound call, has a natural back-and-forth conversation to collect booking details, and hands the owner a ready-to-follow-up lead — no missed calls, no voicemail.

Built solo end-to-end: telephony integration, conversational state machine, multi-tenant data model, and a live real-time demo page.

## What it does

1. A customer calls the business's Twilio number.
2. The AI receptionist answers and asks what they need.
3. Over a few natural turns, it extracts the caller's name, requested service, and preferred time (plus optional fields like staff preference) — asking one clarifying question at a time instead of running a rigid script.
4. Once it has what it needs, it saves the lead, texts the customer a booking link and the owner a summary, and ends the call politely.
5. The lead shows up instantly on a live dashboard / demo feed via WebSocket.

## Why it's interesting from an engineering standpoint

- **Conversational state machine over telephony.** Twilio's `Gather`/webhook model is stateless per request; the app tracks per-call progress (`app/routes/voice.py`) across turns, merges newly-extracted fields into what's already known, and decides whether to ask another question or complete the call.
- **LLM extraction with a real fallback path.** `app/services/ai_service.py` prompts OpenAI to return structured JSON (fields + a natural-sounding follow-up question) every turn. If the API call fails or returns malformed JSON, regex-based extractors kick in so a booking is never silently lost to an upstream error.
- **Config-driven, multi-tenant conversation flow.** `Business` / `BusinessConfig` models let each business customize its greeting, required fields, prompts, services, and staff list from the database — adding a new business doesn't require touching conversation logic.
- **Real-time visibility.** A lightweight WebSocket broadcast manager (`app/websocket_manager.py`) pushes `call_started` / `new_lead` / `call_ended` events to any connected client, powering both a live public demo page and an internal dashboard, with PII masked on the public feed.
- **Graceful degradation on voice.** Optional ElevenLabs TTS for a more natural voice, with automatic fallback to Twilio's built-in `<Say>` if generation fails or the feature is disabled.

## Performance

Voice conversations live or die on latency — a caller will tolerate maybe a second
of silence before it feels broken. Early versions took ~[4]s per turn, which made
the conversation feel like talking to a phone tree. Getting it to feel natural took
a few deliberate changes:

- **Model swap.** Moved extraction from [gpt-4] to gpt-4o-mini — the structured
  JSON extraction task doesn't need frontier-model reasoning, and the smaller model
  cut LLM response time by ~[X]% with no measurable drop in extraction accuracy.
- **Prompt slimming.** Trimmed the system prompt to the minimum needed for reliable
  field extraction, reducing input tokens per turn.
- **TTS caching.** Repeated phrases (greetings, confirmations) are cached in memory
  by `voice_service.py`, so ElevenLabs generation only happens once per unique line.

Current per-turn response time: **~[1.5]s** from end of caller speech to AI reply.

## Testing

Validated against a 55-scenario stress test covering the ways real phone calls go
sideways:

- **Conversation edge cases** — interruptions, long silences, mumbled or
  unintelligible speech, callers changing their answer mid-call
- **Ambiguous bookings** — vague times ("sometime next week?"), multiple services
  in one sentence, missing required fields
- **Hostile / off-script callers** — off-topic questions, profanity, attempts to
  pull the AI out of its receptionist role
- **Failure injection** — OpenAI timeouts and malformed JSON responses (exercising
  the regex fallback path), ElevenLabs failures (exercising the `<Say>` fallback),
  mid-call hangups

Result: **[52/55]** scenarios handled correctly; the remaining [3] are documented
in [issues / the test log] with root causes identified.

## Tech stack

| Layer | Choice |
|---|---|
| API framework | FastAPI (Python) |
| ORM / models | SQLModel (SQLAlchemy) |
| Telephony | Twilio Programmable Voice + SMS, TwiML |
| AI extraction | OpenAI (structured JSON responses) |
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
Lead persisted (SQLModel) ──► SMS to customer + owner ──► WebSocket broadcast
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

- Call state (`CALL_STATE` in `voice.py`) is in-process memory, which is fine for a single Railway instance but wouldn't survive horizontal scaling — a Redis-backed store is the natural next step, noted directly in the code.
- Outbound SMS calls are currently stubbed out (commented) in the demo build to avoid sending real texts from a portfolio deployment; `twilio_service.send_sms` is fully implemented and ready to re-enable.
