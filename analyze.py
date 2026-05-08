"""
analyze.py  —  GabiBot Chat
----------------------------
Single-file Flask app.  On startup it reads ai/flow.json + the unique
screenshots, builds a hidden system prompt that describes the recorded
application, then exposes a minimal chat UI at http://localhost:5000.

Requirements:
    pip install flask openai

Environment variable required:
    OPENAI_API_KEY=sk-...

Run:
    python analyze.py
    python analyze.py --flow ai/flow.json --model gpt-4o
"""

import os
import sys
import json
import base64
import argparse
from pathlib import Path
from flask import Flask, request, jsonify, render_template_string


def load_dotenv_file(env_path: str = ".env"):
    """Minimal .env loader so AZURE_OPENAI_* values can be used without extra deps."""
    if not os.path.exists(env_path):
        return

    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")

                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError as e:
        print(f"[-] Could not read .env file ({env_path}): {e}")


load_dotenv_file(".env")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="GabiBot Chat UI")
parser.add_argument("--flow",  default="ai/flow.json",    help="Path to flow.json")
parser.add_argument("--model", default=os.getenv("AZURE_OPENAI_MODEL", "gpt-5"),           help="OpenAI model to use")
parser.add_argument("--port",  default=5000,  type=int,   help="Port (default 5000)")
args = parser.parse_args()

# ---------------------------------------------------------------------------
# Load flow + encode screenshots
# ---------------------------------------------------------------------------
def _encode(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except OSError:
        return None

def _build_system_prompt(flow_path: str) -> tuple[list, str]:
    """
    Returns (openai_system_content_list, app_name).
    The content list may contain text + image_url blocks.
    """
    if not os.path.exists(flow_path):
        print(f"[-] {flow_path} not found — run orchestrator first.", file=sys.stderr)
        sys.exit(1)

    with open(flow_path, encoding="utf-8") as f:
        flow = json.load(f)

    app_name = flow.get("app", "the application")
    screens  = flow.get("screens", [])

    intro = (
        f"You are an expert assistant that helps users understand and navigate "
        f"'{app_name}'.\n"
        f"You have been given annotated screenshots and a structured interaction log "
        f"from an automated exploration session recorded on {flow.get('session_date','')}.  "
        f"Use this knowledge to answer any question the user has about the application — "
        f"explain features, guide through tasks, and describe what each screen does.\n\n"
        f"Session overview: {flow['total_unique_screens']} unique screen(s) recorded.\n"
    )

    content: list = [{"type": "text", "text": intro}]

    for s in screens:
        # --- text block for this screen ---
        trigger_str = (
            f"{s['trigger']['action']} on \"{s['trigger']['name']}\""
            if s.get("trigger") else "initial load"
        )
        interactions = s.get("interactions_performed", [])
        itext = (
            ", ".join(f"{i['action']} '{i['name']}'" for i in interactions)
            if interactions else "none"
        )
        content.append({
            "type": "text",
            "text": (
                f"\n[Screen {s['screen_index']}]  "
                f"Opened by: {trigger_str}  |  "
                f"Elements: {s['element_count']}  |  "
                f"Interactions: {itext}"
            ),
        })

        # --- image block ---
        img = s.get("image", "")
        b64 = _encode(img) if img and Path(img).exists() else None
        if b64:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url":    f"data:image/png;base64,{b64}",
                    "detail": "high",
                },
            })

    return content, app_name


print("[*] Loading flow …")
SYSTEM_CONTENT, APP_NAME = _build_system_prompt(args.flow)
print(f"[*] System prompt ready  ({len(SYSTEM_CONTENT)} content block(s), model={args.model})")

# ---------------------------------------------------------------------------
# OpenAI client
# ---------------------------------------------------------------------------
try:
    from openai import OpenAI
except ImportError:
    print("[-] openai not installed.  Run:  pip install openai", file=sys.stderr)
    sys.exit(1)

endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "")
api_key = os.getenv("AZURE_OPENAI_API_KEY", "")


def _normalize_openai_base_url(raw_endpoint: str) -> str:
    """Accept plain Azure endpoint and convert it to the OpenAI-compatible base URL."""
    endpoint_clean = raw_endpoint.strip().rstrip("/")
    if not endpoint_clean:
        return endpoint_clean
    if endpoint_clean.endswith("/openai/v1"):
        return f"{endpoint_clean}/"
    return f"{endpoint_clean}/openai/v1/"

if endpoint and api_key:
    base_url = _normalize_openai_base_url(endpoint)
    client = OpenAI(base_url=base_url, api_key=api_key)
    print(f"[*] Using custom Azure OpenAI endpoint: {base_url}")
elif api_key:
    client = OpenAI(api_key=api_key)
    print(f"[*] Using custom API key from environment")
else:
    client = OpenAI()   # reads from OPENAI_API_KEY environment variable
    print(f"[*] Using default OpenAI settings")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)

# Conversation history per session is kept server-side in memory.
# For a single-user local tool this is perfectly fine.
_history: list[dict] = []


HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GabiBot · {{ app_name }}</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0f1117;
    color: #e4e4e7;
    display: flex;
    flex-direction: column;
    height: 100dvh;
  }

  header {
    padding: 14px 24px;
    border-bottom: 1px solid #27272a;
    display: flex;
    align-items: center;
    gap: 10px;
    background: #18181b;
  }
  header .dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: #22c55e;
    box-shadow: 0 0 6px #22c55e88;
  }
  header h1 { font-size: 1rem; font-weight: 600; letter-spacing: .02em; }
  header span { font-size: .8rem; color: #71717a; }

  #chat {
    flex: 1;
    overflow-y: auto;
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 16px;
    scroll-behavior: smooth;
  }

  .bubble {
    max-width: 72%;
    padding: 12px 16px;
    border-radius: 14px;
    line-height: 1.55;
    font-size: .93rem;
    white-space: pre-wrap;
    word-break: break-word;
    animation: pop .18s ease;
  }
  @keyframes pop { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:none; } }

  .bubble.user {
    align-self: flex-end;
    background: #2563eb;
    color: #fff;
    border-bottom-right-radius: 4px;
  }
  .bubble.assistant {
    align-self: flex-start;
    background: #27272a;
    color: #e4e4e7;
    border-bottom-left-radius: 4px;
  }
  .bubble.thinking {
    align-self: flex-start;
    background: #27272a;
    color: #71717a;
    font-style: italic;
  }

  #input-row {
    padding: 16px 24px;
    border-top: 1px solid #27272a;
    background: #18181b;
    display: flex;
    gap: 10px;
    align-items: flex-end;
  }
  #input-row textarea {
    flex: 1;
    resize: none;
    background: #27272a;
    border: 1px solid #3f3f46;
    border-radius: 10px;
    padding: 10px 14px;
    color: #e4e4e7;
    font-size: .93rem;
    font-family: inherit;
    line-height: 1.5;
    max-height: 140px;
    outline: none;
    transition: border-color .15s;
    overflow-y: auto;
  }
  #input-row textarea:focus { border-color: #2563eb; }
  #input-row button {
    background: #2563eb;
    border: none;
    border-radius: 10px;
    padding: 10px 18px;
    color: #fff;
    font-size: .93rem;
    font-weight: 600;
    cursor: pointer;
    transition: background .15s, opacity .15s;
    white-space: nowrap;
  }
  #input-row button:hover { background: #1d4ed8; }
  #input-row button:disabled { opacity: .45; cursor: default; }

  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #3f3f46; border-radius: 3px; }
</style>
</head>
<body>
<header>
  <div class="dot"></div>
  <h1>GabiBot</h1>
  <span>· {{ app_name }}</span>
</header>

<div id="chat"></div>

<div id="input-row">
  <textarea id="msg" rows="1" placeholder="Ask anything about {{ app_name }} …"></textarea>
  <button id="send">Send</button>
</div>

<script>
const chat  = document.getElementById('chat');
const msg   = document.getElementById('msg');
const send  = document.getElementById('send');

function addBubble(role, text) {
  const div = document.createElement('div');
  div.className = `bubble ${role}`;
  div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

msg.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
});
msg.addEventListener('input', () => {
  msg.style.height = 'auto';
  msg.style.height = Math.min(msg.scrollHeight, 140) + 'px';
});
send.addEventListener('click', sendMsg);

async function sendMsg() {
  const text = msg.value.trim();
  if (!text) return;

  msg.value = '';
  msg.style.height = 'auto';
  send.disabled = true;

  addBubble('user', text);
  const thinking = addBubble('thinking', 'Thinking …');

  try {
    const res  = await fetch('/chat', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ message: text }),
    });
    const data = await res.json();
    thinking.remove();
    if (data.error) {
      addBubble('assistant', '⚠ ' + data.error);
    } else {
      addBubble('assistant', data.reply);
    }
  } catch (err) {
    thinking.remove();
    addBubble('assistant', '⚠ Network error: ' + err.message);
  }

  send.disabled = false;
  msg.focus();
}

// Greeting
addBubble('assistant', 'Hi! I have analysed the recorded {{ app_name }} session. Ask me anything about how the app works, what features it has, or how to complete a specific task.');
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(HTML, app_name=APP_NAME)


@app.post("/chat")
def chat_endpoint():
    body = request.get_json(force=True, silent=True) or {}
    user_message = (body.get("message") or "").strip()
    if not user_message:
        return jsonify({"error": "Empty message"}), 400

    _history.append({"role": "user", "content": user_message})

    try:
        response = client.chat.completions.create(
            model=args.model,
            messages=[
                {"role": "system", "content": SYSTEM_CONTENT},
                *_history,
            ],
        )
        content = response.choices[0].message.content
        if isinstance(content, str):
            reply = content.strip()
        elif isinstance(content, list):
            text_chunks = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_chunks.append(block.get("text", ""))
            reply = "\n".join(chunk.strip() for chunk in text_chunks if chunk and chunk.strip())
        else:
            reply = ""

        if not reply:
            reply = "I could not generate a text response for that request."
    except Exception as e:
        print(f"[-] /chat failed: {type(e).__name__}: {e}", file=sys.stderr)
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    _history.append({"role": "assistant", "content": reply})
    return jsonify({"reply": reply})


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"[*] Chat UI  →  http://localhost:{args.port}")
    app.run(host="127.0.0.1", port=args.port, debug=False)
