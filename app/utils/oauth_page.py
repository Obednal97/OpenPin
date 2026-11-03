"""The page the browser shows when Notion or Google Drive sends the user back.

The OAuth callback servers in app/state/notion.py and app/state/gdrive.py
answer with ``send(handler, service, outcome, detail)``. The page is fully
self-contained: no fonts, images or scripts from anywhere else (the URL holds
the one-time code, and nothing should carry it off as a Referer), and its only
script removes that code from the address bar.

Outcomes:
- ``connected``: the code was exchanged; the user goes back to the app.
- ``cancelled``: the user declined on the provider's page (``access_denied``).
- ``expired``: the state didn't match: an old tab, or a restart in between.
- ``failed``: the provider returned an error, or the token exchange failed.
"""

from __future__ import annotations

import html
import secrets

OUTCOMES = ("connected", "cancelled", "expired", "failed")

# Copy per service. "where" is the noun the user picks next in Settings.
_SERVICES = {
    "notion": {
        "name": "Notion",
        "connect": "Connect Notion",
        "next": "Go back to Open Pin and choose the Notion database for your transcripts and summaries.",
    },
    "gdrive": {
        "name": "Google Drive",
        "connect": "Connect Google Drive",
        "next": "Go back to Open Pin and choose the Google Drive folder for your recordings.",
    },
}

_SETTINGS_PATH = "Settings\u00a0›\u00a0Integrations"

# Voice-memo waveform between the two tiles (bar heights in px, symmetric
# around the centre line). The bars grow left to right on success.
_BARS = (4, 8, 14, 10, 20, 28, 18, 32, 24, 30, 16, 22, 12, 18, 8, 10, 4)

# The Open Pin mark (white V holding two dots) on its black tile, 72x72.
_MARK_TILE = (
    '<svg class="tile tile-app" viewBox="0 0 72 72" aria-hidden="true">'
    '<rect width="72" height="72" rx="17" fill="#1d1d1f"/>'
    '<path d="M16 18h7l7 28q.3 1.2 1.5 1.2h9q1.2 0 1.5-1.2l7-28h7l-8.6 30q-2.3 6.5-11.4 6.5t-11.4-6.5z" fill="#fff"/>'
    '<circle cx="36" cy="29" r="4.3" fill="#fff"/>'
    '<circle cx="36" cy="38.4" r="4.3" fill="#fff"/>'
    "</svg>"
)

# Where things go: a page (Notion) or a folder (Google Drive). Generic
# glyphs, not the providers' logos.
_DEST_GLYPHS = {
    "notion": (
        '<path d="M27 20h13l7 7v25a2 2 0 0 1-2 2H27a2 2 0 0 1-2-2V22a2 2 0 0 1 2-2z" class="glyph-line"/>'
        '<path d="M40 20v7h7" class="glyph-line"/>'
        '<path d="M30 36h12M30 41h12M30 46h8" class="glyph-line"/>'
    ),
    "gdrive": (
        '<path d="M20 27a3 3 0 0 1 3-3h8l3.5 4H49a3 3 0 0 1 3 3v16a3 3 0 0 1-3 3H23a3 3 0 0 1-3-3z" class="glyph-line"/>'
        '<path d="M20 33h32" class="glyph-line"/>'
    ),
}

_BADGES = {
    "connected": '<path d="M7.5 12.4l3 3 6-6.6" class="badge-mark"/>',
    "cancelled": '<path d="M8 12h8" class="badge-mark"/>',
    "expired": '<path d="M12 7.5V12l3 2" class="badge-mark"/>',
    "failed": '<path d="M12 7v6" class="badge-mark"/><circle cx="12" cy="16.6" r="1.25" class="badge-dot"/>',
}

_FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 72 72'%3E"
    "%3Crect width='72' height='72' rx='17' fill='%231d1d1f'/%3E"
    "%3Ccircle cx='36' cy='29' r='4.3' fill='%23fff'/%3E%3Ccircle cx='36' cy='38.4' r='4.3' fill='%23fff'/%3E"
    "%3Cpath d='M16 18h7l7 28q.3 1.2 1.5 1.2h9q1.2 0 1.5-1.2l7-28h7l-8.6 30q-2.3 6.5-11.4 6.5t-11.4-6.5z' fill='%23fff'/%3E"
    "%3C/svg%3E"
)


def _copy(service: str, outcome: str) -> tuple[str, str, str]:
    """(tab title, heading, body) for one outcome."""
    s = _SERVICES[service]
    name = s["name"]
    again = f"To try again, open {_SETTINGS_PATH} in Open Pin and choose {s['connect']}."
    if outcome == "connected":
        return (
            f"Connected to {name}",
            f"Open Pin is connected to {name}",
            f"{s['next']} You can close this tab.",
        )
    if outcome == "cancelled":
        return (
            f"{name} not connected",
            f"{name} wasn't connected",
            f"Access was declined on {name}'s page, so nothing changed in Open Pin. {again}",
        )
    if outcome == "expired":
        return (
            "Link out of date",
            "This connection link is out of date",
            f"It belongs to an earlier attempt to connect {name}, so Open Pin ignored it. {again}",
        )
    return (
        f"{name} not connected",
        f"{name} didn't finish connecting",
        f"Open Pin couldn't finish the sign-in with {name}'s reply. {again} "
        "If it keeps failing, use Report a bug in Settings.",
    )


def _waveform(outcome: str) -> str:
    """The bars between the tiles: a full waveform on success, a broken flat line otherwise."""
    width, mid, gap = 132, 36, 7.5
    x0 = (width - gap * (len(_BARS) - 1)) / 2
    bars = []
    for i, h in enumerate(_BARS):
        x = x0 + i * gap
        if outcome == "connected":
            y1, y2 = mid - h / 2, mid + h / 2
        else:
            if outcome != "cancelled" and 6 <= i <= 10:
                continue  # the break in the line
            y1 = y2 = mid
        bars.append(f'<line x1="{x:.1f}" y1="{y1:.1f}" x2="{x:.1f}" y2="{y2:.1f}" class="bar" style="--i:{i}"/>')
    return f'<svg class="wave" viewBox="0 0 {width} 72" aria-hidden="true">{"".join(bars)}</svg>'


def render(service: str, outcome: str, detail: str | None = None, *, nonce: str = "") -> str:
    """The full HTML page. ``detail`` (technical, optional) is escaped and folded away."""
    if service not in _SERVICES:
        raise ValueError(f"unknown service {service!r}")
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    title, heading, body = _copy(service, outcome)
    name = _SERVICES[service]["name"]
    details = ""
    if detail and outcome != "connected":
        details = (
            '<details class="details"><summary>Technical details</summary>'
            f"<pre>{html.escape(str(detail)[:600])}</pre></details>"
        )
    status_label = {
        "connected": f"Open Pin linked to {name}",
        "cancelled": f"Open Pin not linked to {name}",
        "expired": f"Open Pin not linked to {name}",
        "failed": f"Open Pin not linked to {name}",
    }[outcome]
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="color-scheme" content="light dark">
<title>{html.escape(title)} · Open Pin</title>
<link rel="icon" href="{_FAVICON}">
<style>
:root {{
  --bg: #f5f5f7;
  --text: #1d1d1f;
  --text-secondary: #6e6e73;
  --surface: #ffffff;
  --hairline: rgba(0, 0, 0, 0.1);
  --wave: #c7c7cc;
  --ok: #28cd41;
  --bad: #ff3b30;
  --muted: #8e8e93;
  --warn: #ff9500;
  --code-bg: rgba(0, 0, 0, 0.04);
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #0d0d0d;
    --text: #f5f5f7;
    --text-secondary: #98989d;
    --surface: #1c1c1e;
    --hairline: rgba(255, 255, 255, 0.12);
    --wave: #48484a;
    --ok: #30d158;
    --bad: #ff453a;
    --muted: #8e8e93;
    --warn: #ff9f0a;
    --code-bg: rgba(255, 255, 255, 0.06);
  }}
}}
* {{ box-sizing: border-box; margin: 0; }}
html {{ background: var(--bg); }}
body {{
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 48px 16px;
  background: var(--bg);
  color: var(--text);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
}}
main {{ width: 100%; max-width: 440px; text-align: center; }}
.link {{
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 auto 36px;
}}
.tile {{ width: 72px; height: 72px; flex: none; display: block; }}
.tile-app {{ filter: drop-shadow(0 6px 14px rgba(0, 0, 0, 0.18)); }}
.dest {{ position: relative; flex: none; }}
.tile-dest rect {{ fill: var(--surface); stroke: var(--hairline); }}
.glyph-line {{
  fill: none;
  stroke: var(--text);
  stroke-width: 2.4;
  stroke-linecap: round;
  stroke-linejoin: round;
}}
.wave {{ width: 132px; height: 72px; flex: none; display: block; }}
.bar {{ stroke: var(--wave); stroke-width: 3.2; stroke-linecap: round; }}
.connected .bar {{ stroke: var(--ok); }}
.badge {{
  position: absolute;
  top: -9px;
  right: -9px;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  border: 3px solid var(--bg);
  box-sizing: content-box;
  display: grid;
  place-items: center;
}}
.connected .badge {{ background: var(--ok); }}
.failed .badge {{ background: var(--bad); }}
.expired .badge {{ background: var(--warn); }}
.cancelled .badge {{ background: var(--muted); }}
.badge svg {{ width: 24px; height: 24px; }}
.badge-mark {{ fill: none; stroke: #fff; stroke-width: 2.4; stroke-linecap: round; stroke-linejoin: round; }}
.badge-dot {{ fill: #fff; }}
h1 {{
  font: 600 27px/1.2 -apple-system, BlinkMacSystemFont, "SF Pro Display", "Helvetica Neue", system-ui, sans-serif;
  letter-spacing: -0.022em;
  text-wrap: balance;
  margin-bottom: 12px;
}}
p {{ color: var(--text-secondary); text-wrap: pretty; max-width: 38ch; margin: 0 auto; }}
.details {{ margin-top: 28px; text-align: left; }}
.details summary {{
  cursor: pointer;
  color: var(--text-secondary);
  font-size: 13px;
  width: max-content;
  margin: 0 auto;
  border-radius: 6px;
  padding: 2px 6px;
}}
.details summary:focus-visible {{ outline: 2px solid var(--text); outline-offset: 2px; }}
.details pre {{
  margin-top: 12px;
  padding: 12px 14px;
  border-radius: 10px;
  background: var(--code-bg);
  color: var(--text-secondary);
  font: 12px/1.5 ui-monospace, "SF Mono", Menlo, monospace;
  white-space: pre-wrap;
  word-break: break-word;
}}
@media (prefers-reduced-motion: no-preference) {{
  .connected .bar {{
    transform-box: fill-box;
    transform-origin: center;
    animation: grow 420ms cubic-bezier(0.3, 0.7, 0.2, 1) backwards;
    animation-delay: calc(var(--i) * 32ms + 120ms);
  }}
  .connected .badge {{ animation: settle 360ms cubic-bezier(0.3, 1.4, 0.5, 1) 760ms backwards; }}
  @keyframes grow {{ from {{ transform: scaleY(0.1); stroke: var(--wave); }} }}
  @keyframes settle {{ from {{ transform: scale(0.4); opacity: 0; }} }}
}}
@media (max-width: 380px) {{
  .wave {{ width: 96px; }}
  h1 {{ font-size: 24px; }}
}}
</style>
</head>
<body>
<main class="{outcome}">
  <div class="link" role="img" aria-label="{html.escape(status_label)}">
    {_MARK_TILE}
    {_waveform(outcome)}
    <div class="dest">
      <svg class="tile tile-dest" viewBox="0 0 72 72" aria-hidden="true">
        <rect x="0.5" y="0.5" width="71" height="71" rx="16.5"/>{_DEST_GLYPHS[service]}
      </svg>
      <span class="badge"><svg viewBox="0 0 24 24" aria-hidden="true">{_BADGES[outcome]}</svg></span>
    </div>
  </div>
  <h1>{html.escape(heading)}</h1>
  <p>{html.escape(body)}</p>
  {details}
</main>
<script nonce="{nonce}">history.replaceState(null, "", location.pathname);</script>
</body>
</html>
"""


def send(handler, service: str, outcome: str, detail: str | None = None) -> None:
    """Write the page as the response of a BaseHTTPRequestHandler."""
    nonce = secrets.token_urlsafe(16)
    body = render(service, outcome, detail, nonce=nonce).encode("utf-8")
    handler.send_response(200 if outcome == "connected" else 400)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header(
        "Content-Security-Policy",
        f"default-src 'none'; style-src 'unsafe-inline'; img-src data:; script-src 'nonce-{nonce}'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    )
    handler.end_headers()
    handler.wfile.write(body)
