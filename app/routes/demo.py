"""
routers/demo.py
---
Three endpoints:

  GET  /demo            → public HTML sales demo page
  WS   /ws/leads        → WebSocket for live lead feed (no auth, demo-safe data only)
  POST /voice/status    → Twilio status callback → broadcasts call_ended

Wire into main.py:
    from app.routers.demo import router as demo_router
    app.include_router(demo_router)

Twilio status callback setup (two options):
  1. Twilio console → Phone Numbers → your number → "Call Status Changes" URL
     → set to https://your-railway-url.up.railway.app/voice/status
  2. Or add statusCallback to your TwiML <Response> if you're generating it programmatically
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from ..websocket_manager import manager  # adjust import path to match your structure
from ..security import verify_twilio_signature

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Phone number from env so you can override per deployment ─────────────────
DEMO_PHONE_DISPLAY = os.getenv("DEMO_PHONE_DISPLAY", "+1 (435) 265-4742")
DEMO_PHONE_TEL = os.getenv("DEMO_PHONE_TEL", "+14352654742")


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@router.websocket("/ws/leads")
async def leads_ws(websocket: WebSocket) -> None:
    """
    No authentication — this streams demo-safe data only (masked phone,
    first name + last initial). Full lead data stays in /dashboard/leads.
    """
    await manager.connect(websocket)
    try:
        while True:
            try:
                # Accept pings from the client keep-alive interval
                data = await asyncio.wait_for(websocket.receive_text(), timeout=60)
                if data == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                # Send a server-side ping to detect dead connections
                await websocket.send_text('{"type":"ping"}')
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception as e:
        logger.warning(f"[ws] unexpected error: {e}")
        await manager.disconnect(websocket)


# ── Twilio status callback ────────────────────────────────────────────────────

@router.post("/voice/status", dependencies=[Depends(verify_twilio_signature)])
async def voice_status(request: Request) -> Response:
    """
    Twilio posts here when call status changes. We use it to broadcast
    call_ended so the demo page can flip the "Call in Progress" banner off.
    """
    form = await request.form()
    call_status = form.get("CallStatus", "")
    call_sid = form.get("CallSid", "")

    logger.info(f"[status] {call_sid} → {call_status}")

    terminal_statuses = {"completed", "failed", "busy", "no-answer", "canceled"}
    if call_status in terminal_statuses:
        asyncio.create_task(
            manager.broadcast({"type": "call_ended", "data": {"status": call_status}})
        )

    return Response(content="", status_code=204)


# ── Demo page ─────────────────────────────────────────────────────────────────

@router.get("/demo", response_class=HTMLResponse)
async def demo_page() -> HTMLResponse:
    html = _build_demo_html(
        phone_display=DEMO_PHONE_DISPLAY,
        phone_tel=DEMO_PHONE_TEL,
    )
    return HTMLResponse(content=html)


def _build_demo_html(phone_display: str, phone_tel: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AI Reception — Live Demo</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}

    :root {{
      --bg:          #08090E;
      --surface:     #0F1117;
      --surface-2:   #161B27;
      --border:      #1C2030;
      --border-hi:   #2D3748;
      --green:       #00C47A;
      --green-dim:   rgba(0,196,122,0.10);
      --green-glow:  rgba(0,196,122,0.18);
      --amber:       #F59E0B;
      --amber-dim:   rgba(245,158,11,0.10);
      --text:        #EEF2FF;
      --text-muted:  #7B8597;
      --text-subtle: #374151;
      --font:        'Space Grotesk', system-ui, sans-serif;
      --mono:        'JetBrains Mono', ui-monospace, monospace;
    }}

    body {{
      background: var(--bg);
      color: var(--text);
      font-family: var(--font);
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
    }}

    /* ── NAV ── */
    nav {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 20px 48px;
      border-bottom: 1px solid var(--border);
      position: sticky;
      top: 0;
      background: rgba(8,9,14,0.85);
      backdrop-filter: blur(12px);
      z-index: 100;
    }}

    .logo {{
      font-size: 15px;
      font-weight: 700;
      letter-spacing: -0.02em;
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .logo-dot {{
      width: 7px; height: 7px;
      border-radius: 50%;
      background: var(--green);
    }}

    .live-badge {{
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 6px 14px;
      border-radius: 100px;
      border: 1px solid var(--border-hi);
      background: var(--surface);
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--green);
      transition: all 0.4s ease;
    }}
    .live-badge.ringing {{
      color: var(--amber);
      border-color: rgba(245,158,11,0.4);
      background: var(--amber-dim);
    }}
    .live-dot {{
      width: 7px; height: 7px;
      border-radius: 50%;
      background: var(--green);
      animation: pulse-dot 2.4s ease-in-out infinite;
    }}
    .live-badge.ringing .live-dot {{
      background: var(--amber);
      animation: pulse-dot 0.75s ease-in-out infinite;
    }}

    @keyframes pulse-dot {{
      0%, 100% {{ opacity: 1; transform: scale(1); }}
      50%       {{ opacity: 0.35; transform: scale(0.8); }}
    }}

    /* ── HERO ── */
    .hero {{
      text-align: center;
      padding: 88px 24px 56px;
      max-width: 640px;
      margin: 0 auto;
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--green);
      margin-bottom: 28px;
    }}
    .eyebrow-line {{
      width: 24px;
      height: 1px;
      background: var(--green);
      opacity: 0.5;
    }}
    h1 {{
      font-size: clamp(38px, 7vw, 60px);
      font-weight: 700;
      line-height: 1.04;
      letter-spacing: -0.035em;
      margin-bottom: 18px;
    }}
    h1 em {{
      font-style: normal;
      background: linear-gradient(135deg, var(--green) 0%, #00E5A0 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
    }}
    .hero-sub {{
      font-size: 17px;
      color: var(--text-muted);
      line-height: 1.65;
      margin-bottom: 52px;
    }}

    /* ── PHONE CARD ── */
    .phone-card {{
      display: inline-flex;
      flex-direction: column;
      align-items: center;
      gap: 10px;
      background: var(--surface);
      border: 1px solid var(--border-hi);
      border-radius: 22px;
      padding: 32px 52px;
      position: relative;
      overflow: hidden;
      cursor: default;
      transition: border-color 0.25s;
    }}
    .phone-card:hover {{
      border-color: var(--green);
      box-shadow: 0 0 40px var(--green-glow);
    }}
    .phone-card::before {{
      content: '';
      position: absolute;
      top: 0; left: 50%;
      transform: translateX(-50%);
      width: 55%;
      height: 1px;
      background: linear-gradient(90deg, transparent, var(--green), transparent);
    }}

    .phone-label {{
      font-size: 10px;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: var(--text-subtle);
    }}
    .phone-number {{
      font-family: var(--mono);
      font-size: clamp(26px, 5vw, 40px);
      font-weight: 500;
      letter-spacing: 0.06em;
      color: var(--text);
      text-decoration: none;
      display: block;
      min-height: 1.2em;  /* prevents layout shift during typewriter */
    }}
    .phone-number:hover {{ color: var(--green); }}
    .phone-cursor {{
      display: inline-block;
      width: 2px;
      background: var(--green);
      animation: blink 0.9s step-end infinite;
      vertical-align: -1px;
      height: 0.85em;
      margin-left: 2px;
    }}
    @keyframes blink {{
      0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0; }}
    }}
    .phone-cta {{
      font-size: 13px;
      color: var(--text-subtle);
      margin-top: 2px;
    }}
    .phone-cta span {{ color: var(--green); }}

    /* ── FEED SECTION ── */
    .feed-section {{
      max-width: 640px;
      margin: 0 auto;
      padding: 0 24px 100px;
    }}

    .feed-header {{
      display: flex;
      align-items: center;
      gap: 16px;
      margin-bottom: 20px;
    }}
    .feed-label {{
      font-size: 10px;
      font-weight: 600;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--text-subtle);
      white-space: nowrap;
    }}
    .feed-rule {{
      flex: 1;
      height: 1px;
      background: var(--border);
    }}

    /* ── CALL BANNER ── */
    .call-banner {{
      display: none;
      align-items: center;
      gap: 12px;
      padding: 14px 20px;
      background: var(--amber-dim);
      border: 1px solid rgba(245,158,11,0.22);
      border-radius: 12px;
      margin-bottom: 14px;
      font-size: 13px;
      color: var(--amber);
      animation: fadeIn 0.3s ease;
    }}
    .call-banner.visible {{ display: flex; }}
    .call-banner-dot {{
      width: 8px; height: 8px;
      border-radius: 50%;
      background: var(--amber);
      flex-shrink: 0;
      animation: pulse-dot 0.75s ease-in-out infinite;
    }}

    /* ── EMPTY STATE ── */
    .empty-state {{
      text-align: center;
      padding: 64px 20px;
      color: var(--text-subtle);
    }}
    .empty-icon {{
      font-size: 28px;
      margin-bottom: 14px;
      display: block;
      opacity: 0.35;
    }}
    .empty-state p {{
      font-size: 15px;
      line-height: 1.7;
    }}

    /* ── LEAD CARD ── */
    .lead-card {{
      display: flex;
      align-items: flex-start;
      gap: 16px;
      padding: 18px 20px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      margin-bottom: 10px;
      animation: slide-in 0.45s cubic-bezier(0.16, 1, 0.3, 1) both;
    }}
    .lead-card.new {{
      border-color: var(--green);
      box-shadow: 0 0 20px var(--green-dim);
    }}

    @keyframes slide-in {{
      from {{ opacity: 0; transform: translateY(-18px); }}
      to   {{ opacity: 1; transform: translateY(0);     }}
    }}
    @keyframes fadeIn {{
      from {{ opacity: 0; }} to {{ opacity: 1; }}
    }}

    .lead-avatar {{
      width: 42px; height: 42px;
      border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: 15px; font-weight: 700;
      flex-shrink: 0;
    }}
    .lead-body {{ flex: 1; min-width: 0; }}
    .lead-name {{
      font-size: 15px; font-weight: 600;
      margin-bottom: 5px;
    }}
    .lead-meta {{
      display: flex; align-items: center;
      gap: 8px; margin-bottom: 5px;
    }}
    .service-pill {{
      display: inline-flex; align-items: center;
      padding: 2px 10px;
      background: var(--green-dim);
      border-radius: 100px;
      font-size: 11px; font-weight: 600;
      color: var(--green);
      letter-spacing: 0.02em;
    }}
    .lead-time-req {{
      font-size: 12px;
      color: var(--text-muted);
    }}
    .lead-phone {{
      font-family: var(--mono);
      font-size: 12px;
      color: var(--text-subtle);
      letter-spacing: 0.04em;
    }}
    .lead-ts {{
      font-size: 11px;
      font-family: var(--mono);
      color: var(--text-subtle);
      white-space: nowrap;
      padding-top: 3px;
    }}

    /* ── HOW IT WORKS ── */
    .how-section {{
      border-top: 1px solid var(--border);
      padding: 64px 24px;
      max-width: 640px;
      margin: 0 auto;
    }}
    .how-title {{
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--text-subtle);
      margin-bottom: 32px;
      text-align: center;
    }}
    .steps {{
      display: flex;
      flex-direction: column;
      gap: 0;
    }}
    .step {{
      display: flex;
      gap: 20px;
      padding-bottom: 28px;
      position: relative;
    }}
    .step:not(:last-child)::after {{
      content: '';
      position: absolute;
      left: 15px; top: 34px;
      width: 1px;
      bottom: 0;
      background: var(--border);
    }}
    .step-num {{
      width: 32px; height: 32px;
      border-radius: 50%;
      background: var(--surface-2);
      border: 1px solid var(--border-hi);
      display: flex; align-items: center; justify-content: center;
      font-size: 12px; font-weight: 700;
      color: var(--green);
      flex-shrink: 0;
      position: relative;
      z-index: 1;
    }}
    .step-body {{ padding-top: 5px; }}
    .step-heading {{ font-size: 14px; font-weight: 600; margin-bottom: 4px; }}
    .step-desc {{ font-size: 13px; color: var(--text-muted); line-height: 1.6; }}

    /* ── FOOTER ── */
    footer {{
      text-align: center;
      padding: 32px;
      border-top: 1px solid var(--border);
      font-size: 12px;
      color: var(--text-subtle);
    }}
    footer a {{ color: var(--text-muted); text-decoration: none; }}
    footer a:hover {{ color: var(--green); }}

    @media (max-width: 600px) {{
      nav {{ padding: 16px 20px; }}
      .hero {{ padding: 60px 20px 40px; }}
      .phone-card {{ padding: 24px 28px; }}
    }}
  </style>
</head>
<body>

  <nav>
    <div class="logo">
      <div class="logo-dot"></div>
      AI Reception
    </div>
    <div class="live-badge" id="liveBadge">
      <div class="live-dot"></div>
      <span id="liveText">LIVE</span>
    </div>
  </nav>

  <section class="hero">
    <div class="eyebrow">
      <div class="eyebrow-line"></div>
      Interactive Demo
      <div class="eyebrow-line"></div>
    </div>
    <h1>Never miss<br>another <em>booking.</em></h1>
    <p class="hero-sub">Call the number below. Your AI receptionist answers, collects your info, and you'll watch your lead appear here in real time.</p>

    <div class="phone-card">
      <div class="phone-label">Call to try it</div>
      <a href="tel:{phone_tel}" class="phone-number" id="phoneNumber"></a>
      <div class="phone-cta">Tap to call &nbsp;·&nbsp; <span>powered by AI</span></div>
    </div>
  </section>

  <section class="feed-section">
    <div class="feed-header">
      <div class="feed-label">Live Activity</div>
      <div class="feed-rule"></div>
    </div>

    <div class="call-banner" id="callBanner">
      <div class="call-banner-dot"></div>
      <span>Call in progress &mdash; your AI receptionist is on the line</span>
    </div>

    <div id="leadFeed">
      <div class="empty-state" id="emptyState">
        <span class="empty-icon">&#128222;</span>
        <p>The line is open.<br>Give it a call &mdash; your info will appear here.</p>
      </div>
    </div>
  </section>

  <section class="how-section">
    <div class="how-title">How it works</div>
    <div class="steps">
      <div class="step">
        <div class="step-num">1</div>
        <div class="step-body">
          <div class="step-heading">Customer calls your number</div>
          <div class="step-desc">Your Twilio number rings straight to the AI. No hold music, no voicemail, no missed calls — it answers every time.</div>
        </div>
      </div>
      <div class="step">
        <div class="step-num">2</div>
        <div class="step-body">
          <div class="step-heading">AI collects booking info</div>
          <div class="step-desc">In about 30 seconds it gets their name, callback number, service, and preferred time — naturally, like a real receptionist.</div>
        </div>
      </div>
      <div class="step">
        <div class="step-num">3</div>
        <div class="step-body">
          <div class="step-heading">Lead lands in your dashboard</div>
          <div class="step-desc">You get an SMS notification and the lead is logged with all details. Confirm and you're done.</div>
        </div>
      </div>
    </div>
  </section>

  <footer>
    Built by <a href="https://strazzo.io" target="_blank">Strazzo LLC</a> &nbsp;&middot;&nbsp; AI receptionists for small business
  </footer>

  <script>
    // ── Phone number typewriter effect ─────────────────────────────────────
    // The signature element: digits appear one-by-one, like dialing in.
    const PHONE_DISPLAY = "{phone_display}";

    (function typewriterPhone() {{
      const el = document.getElementById('phoneNumber');
      el.textContent = '';

      // Add blinking cursor while typing
      const cursor = document.createElement('span');
      cursor.className = 'phone-cursor';
      el.appendChild(cursor);

      let i = 0;
      const interval = setInterval(() => {{
        el.insertBefore(document.createTextNode(PHONE_DISPLAY[i]), cursor);
        i++;
        if (i >= PHONE_DISPLAY.length) {{
          clearInterval(interval);
          // Cursor stays blinking after done — invites interaction
        }}
      }}, 55);
    }})();

    // ── Avatar colors (consistent hash per name) ───────────────────────────
    const AVATAR_COLORS = [
      ['#1D4ED8', '#1e3a8a20'],
      ['#7C3AED', '#4c1d9520'],
      ['#DB2777', '#83184420'],
      ['#D97706', '#92400e20'],
      ['#059669', '#065f4620'],
      ['#DC2626', '#7f1d1d20'],
    ];

    function hashName(name) {{
      let h = 0;
      for (const c of name) h = (h * 31 + c.charCodeAt(0)) & 0xFFFFFF;
      return Math.abs(h) % AVATAR_COLORS.length;
    }}

    function initials(name) {{
      return name.trim().split(/\s+/).map(p => p[0]).join('').toUpperCase().slice(0, 2);
    }}

    function maskPhone(raw) {{
      const digits = (raw || '').replace(/\D/g, '');
      if (digits.length >= 10) {{
        return `+1 (***) ***-${{digits.slice(-4)}}`;
      }}
      return '(***) ***-****';
    }}

    function relativeTime(isoStr) {{
      const diff = Math.floor((Date.now() - new Date(isoStr)) / 1000);
      if (diff < 8)   return 'just now';
      if (diff < 60)  return `${{diff}}s ago`;
      if (diff < 3600) return `${{Math.floor(diff / 60)}}m ago`;
      return new Date(isoStr).toLocaleTimeString([], {{ hour: '2-digit', minute: '2-digit' }});
    }}

    // ── Add lead card to feed ──────────────────────────────────────────────
    function addLeadCard(data) {{
      const feed = document.getElementById('leadFeed');
      const empty = document.getElementById('emptyState');
      if (empty) empty.remove();

      const name = data.name || 'Unknown Caller';
      const idx  = hashName(name);
      const [fg, bg] = AVATAR_COLORS[idx];

      const card = document.createElement('div');
      card.className = 'lead-card new';
      card.innerHTML = `
        <div class="lead-avatar" style="background:${{bg}};color:${{fg}}">${{initials(name)}}</div>
        <div class="lead-body">
          <div class="lead-name">${{name}}</div>
          <div class="lead-meta">
            ${{data.service ? `<span class="service-pill">${{data.service}}</span>` : ''}}
            ${{data.preferred_time ? `<span class="lead-time-req">${{data.preferred_time}}</span>` : ''}}
          </div>
          <div class="lead-phone">${{maskPhone(data.phone)}}</div>
        </div>
        <div class="lead-ts">${{relativeTime(data.timestamp || new Date().toISOString())}}</div>
      `;

      feed.insertBefore(card, feed.firstChild);

      // Remove "new" highlight after 4s
      setTimeout(() => card.classList.remove('new'), 4000);
    }}

    // ── Call in progress state ─────────────────────────────────────────────
    function setCallActive(active) {{
      const badge  = document.getElementById('liveBadge');
      const text   = document.getElementById('liveText');
      const banner = document.getElementById('callBanner');

      if (active) {{
        badge.classList.add('ringing');
        text.textContent = 'CALL IN PROGRESS';
        banner.classList.add('visible');
      }} else {{
        badge.classList.remove('ringing');
        text.textContent = 'LIVE';
        banner.classList.remove('visible');
      }}
    }}

    // ── WebSocket with auto-reconnect ──────────────────────────────────────
    let pingInterval;

    function connect() {{
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws    = new WebSocket(`${{proto}}//${{location.host}}/ws/leads`);

      ws.onopen = () => {{
        console.log('[ws] connected');
        pingInterval = setInterval(() => {{
          if (ws.readyState === WebSocket.OPEN) ws.send('ping');
        }}, 25000);
      }};

      ws.onmessage = (e) => {{
        let msg;
        try {{ msg = JSON.parse(e.data); }} catch {{ return; }}
        if (msg.type === 'new_lead')     addLeadCard(msg.data);
        if (msg.type === 'call_started') setCallActive(true);
        if (msg.type === 'call_ended')   setCallActive(false);
      }};

      ws.onclose = () => {{
        console.log('[ws] closed — reconnecting in 3s');
        clearInterval(pingInterval);
        setTimeout(connect, 3000);
      }};

      ws.onerror = () => ws.close();
    }}

    connect();
  </script>

</body>
</html>"""
