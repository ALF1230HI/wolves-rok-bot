"""AI-generated first response for support tickets, with ongoing conversation.

Shared by both bot.py (the live Discord bot) and webapp.py (the donation
website's "Get Help" flow) so a ticket gets the same automated help
whichever way it was opened. Uses Google's Gemini API directly over aiohttp
(already a dependency everywhere else in this project) rather than adding
the official SDK as a new dependency. Gemini's free tier requires no
credit card and no billing, so this runs at zero ongoing cost.

bot.py and webapp.py run in the same process (webapp's aiohttp app is
started as a task inside bot.py's event loop), so the in-memory
TICKET_SESSIONS dict here is shared between however a ticket was opened and
the bot's own on_message listener that keeps the conversation going --
there's only ever one real gateway connection to Discord, so follow-up
messages are always seen by bot.py regardless of which path created the
ticket. This state lives in memory only, so it resets on a bot restart; a
restart mid-conversation just means the AI stops replying to that thread
until the human helper (already pinged at least once per ticket) steps in.

If anything goes wrong -- no API key configured, a network error, a bad
response -- this fails safe to `confident: False`, so the human helper
always still gets looped in rather than a ticket silently going
unanswered.
"""

import os
import json

from aiohttp import ClientSession, ClientTimeout

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
GEMINI_API = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# The human who gets looped in whenever the AI can't confidently resolve a
# ticket itself, or the reporter says the suggestion didn't help.
HUMAN_HELPER_USER_ID = int(os.getenv("TICKET_HELPER_USER_ID", "1504192100013703418"))

# thread_id -> {"opener_id": int, "history": [(role, text), ...], "ai_active": bool}
# "history" alternates ("user", <member's message>) / ("model", <AI's reply>).
# "ai_active" goes False once the member clicks "Still need help?" or anyone
# other than the ticket opener (i.e. a human helper) posts in the thread.
TICKET_SESSIONS: dict = {}

# How many of the most recent turns to actually send to the model. Keeps
# latency and token usage bounded on long-running tickets; the full history
# is still kept in memory for reference.
MAX_HISTORY_TURNS = 12

SYSTEM_PROMPT = """You are a support assistant for a Rise of Kingdoms alliance's Discord bot. \
The bot has two member-facing features people can have trouble with:

1. /donate - running it in Discord gives the member a personal link (expires 30 minutes \
after being generated) to a web page. There they pick how much Food/Wood/Stone/Gold they \
donated, upload a screenshot of their in-game Alliance -> Assistance -> Assistance Report \
entry as proof, and submit. That writes a row to the alliance's donation spreadsheet and \
posts a confirmation back in the Discord channel they ran /donate from.
2. /ticket - opens the support ticket you're replying in right now, for anything that isn't \
working.

Common real issues and their fixes: the /donate link expired (links only last 30 minutes -- \
just run /donate again in Discord for a fresh one), the screenshot upload fails (should be a \
normal image file, jpg/png, not too large), the submit button seems to do nothing (usually \
means no resource amount above 0 was entered, or no screenshot was attached -- both are \
required), or the member is unsure what counts as proof (it must be the Assistance Report \
entry for that specific donation, not a different screenshot).

This is a support ticket, so you may be replying more than once as the conversation \
continues -- use the earlier turns for context instead of repeating yourself, and ask a \
clarifying question if you need more detail to actually help.

Given the conversation so far, write a short, friendly, specific reply (3-5 sentences, plain \
text, no markdown headers) that actually tries to solve the member's problem. Then decide: \
can a member realistically fix this themselves from your reply, or does this need a human \
(e.g. a bug, a spreadsheet/permissions problem, or anything you don't have enough information \
to solve confidently)?

Respond with ONLY valid JSON in this exact shape:
{"reply": "<your reply text>", "confident": true or false}"""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "reply": {"type": "STRING"},
        "confident": {"type": "BOOLEAN"},
    },
    "required": ["reply", "confident"],
}

FALLBACK_RESULT = {
    "reply": (
        "Thanks for the details! I wasn't able to put together a suggestion for this one "
        "— looping in a human to help."
    ),
    "confident": False,
}


async def _generate(contents: list) -> dict:
    """Shared Gemini call. `contents` is a list of {"role", "parts"} turns,
    already in Gemini's wire format. Returns {"reply": str, "confident": bool},
    failing safe to FALLBACK_RESULT on any problem."""
    if not GEMINI_API_KEY:
        print("[ai_helper] no GEMINI_API_KEY set, skipping AI response")
        return dict(FALLBACK_RESULT)

    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "maxOutputTokens": 300,
            "temperature": 0.4,
            # Keep latency down: this is a short classification + reply task,
            # not something that benefits from extended "thinking" tokens.
            "thinkingConfig": {"thinkingLevel": "low"},
        },
    }
    try:
        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            async with session.post(GEMINI_API, json=body, headers=headers) as resp:
                if resp.status >= 300:
                    text = await resp.text()
                    print(f"[ai_helper] Gemini request failed ({resp.status}): {text}")
                    return dict(FALLBACK_RESULT)
                data = await resp.json()
        content = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(content)
        reply = str(parsed.get("reply", "")).strip()
        confident = bool(parsed.get("confident", False))
        if not reply:
            return dict(FALLBACK_RESULT)
        return {"reply": reply, "confident": confident}
    except Exception as e:
        print(f"[ai_helper] error getting AI response: {e!r}")
        return dict(FALLBACK_RESULT)


async def get_ai_response(issue: str) -> dict:
    """First response to a freshly-opened ticket. Returns {"reply": str, "confident": bool}."""
    return await _generate([{"role": "user", "parts": [{"text": issue}]}])


async def get_ai_followup(history: list) -> dict:
    """Continues a ticket conversation. `history` is a list of (role, text)
    tuples, role being "user" or "model", oldest first. Returns
    {"reply": str, "confident": bool}."""
    turns = history[-MAX_HISTORY_TURNS:]
    contents = [{"role": role, "parts": [{"text": text}]} for role, text in turns if text]
    if not contents:
        return dict(FALLBACK_RESULT)
    return await _generate(contents)


def escalate_mention() -> str:
    return f"<@{HUMAN_HELPER_USER_ID}>"


def still_need_help_component(thread_id) -> dict:
    """Raw Discord message-component JSON for the 'Still need help?' button,
    for use in plain REST message-creation calls (webapp.py)."""
    return {
        "type": 1,
        "components": [
            {
                "type": 2,
                "style": 2,
                "label": "🙋 Still need help?",
                "custom_id": f"escalate_ticket:{thread_id}",
            }
        ],
    }


# --- Ticket conversation state -------------------------------------------------

def start_session(thread_id: int, opener_id: int, issue: str, first_reply: str) -> None:
    """Called right after a ticket's first AI response is posted, by whichever
    path created the ticket (/ticket or the website)."""
    TICKET_SESSIONS[int(thread_id)] = {
        "opener_id": int(opener_id),
        "history": [("user", issue), ("model", first_reply)],
        "ai_active": True,
    }


def get_session(thread_id: int) -> dict:
    return TICKET_SESSIONS.get(int(thread_id))


def set_session(thread_id: int, opener_id: int, history: list, ai_active: bool = True) -> dict:
    """Used to (re)build a session from scratch, e.g. when bootstrapping one
    from Discord message history after a restart."""
    session = {
        "opener_id": int(opener_id),
        "history": list(history),
        "ai_active": ai_active,
    }
    TICKET_SESSIONS[int(thread_id)] = session
    return session


def append_turn(thread_id: int, role: str, text: str) -> None:
    session = TICKET_SESSIONS.get(int(thread_id))
    if session is not None and text:
        session["history"].append((role, text))


def pause_session(thread_id: int) -> None:
    session = TICKET_SESSIONS.get(int(thread_id))
    if session is not None:
        session["ai_active"] = False


def is_ai_active(thread_id: int) -> bool:
    session = TICKET_SESSIONS.get(int(thread_id))
    return bool(session and session.get("ai_active", False))
