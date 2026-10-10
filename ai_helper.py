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

SYSTEM_PROMPT = """You are a friendly, conversational support assistant for a Rise of Kingdoms \
alliance's Discord bot. The bot has two member-facing features people can have trouble with:

1. /donate - running it in Discord gives the member a personal link (expires 30 minutes after \
being generated) to a web page. The step-by-step flow there is:
   Step 1. In-game, go to Alliance -> Assistance and send the resources being donated to the \
   alliance bank shown on the page.
   Step 2. Open Alliance -> Assistance -> Assistance Report and screenshot the entry showing \
   exactly what was just sent.
   Step 3. On the web page, enter the exact Food/Wood/Stone/Gold amounts donated, attach that \
   screenshot, and submit.
   Step 4. The donation is logged to the alliance's tracker and a confirmation is posted back \
   in the Discord channel /donate was run from.
2. /ticket - opens the support ticket you're replying in right now, for anything that isn't \
working. A member can also open one from the website via a "Get Help" link.

Common real issues and their fixes: the /donate link expired (links only last 30 minutes -- \
just run /donate again in Discord for a fresh one), the screenshot upload fails (should be a \
normal image file, jpg/png, under about 20MB), the submit button seems to do nothing (usually \
means no resource amount above 0 was entered, or no screenshot was attached -- both are \
required), or the member is unsure what counts as proof (it must be the Assistance Report \
entry for that specific donation, not a different screenshot).

How to handle each message, in order of priority:
- If the member's message is vague or just a general request for help (e.g. "help", "I need a \
guide", "it's not working", or anything that doesn't name a specific symptom), do NOT just ask \
what's wrong. Immediately walk them through the step-by-step /donate guide above (it's the \
most common reason people open a ticket), in your own words, and ask them to try it and say \
what happens, or tell you if they meant something else. Giving that guide counts as helping --
set confident:true for this.
- If they've described a specific symptom, give the specific fix for it from the list above, \
specific and actionable. This also counts as confident:true.
- Only ask a clarifying question instead of giving a guide/fix if you genuinely have no idea \
which feature they even mean after reading the whole conversation so far -- and even then, \
still set confident:true, since you're actively helping move the conversation forward.
- Set confident:false ONLY when a human genuinely needs to look at something you cannot fix \
with advice: an actual bug, a wrong number already recorded in the spreadsheet, a permissions \
problem, or the member has clearly said they already tried your exact guide/fix and it still \
didn't work. Simply not having enough detail yet, or it being the first message, is never a \
reason for confident:false -- try the guide first.

Write a short, friendly reply (3-6 sentences, plain text, no markdown headers) using the \
conversation so far for context so you don't repeat yourself turn over turn.

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
        "escalated": False,
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
        "escalated": False,
    }
    TICKET_SESSIONS[int(thread_id)] = session
    return session


def should_ping_human(thread_id: int) -> bool:
    """Whether a not-confident reply should actually ping the human helper
    right now. Only pings once per ticket -- after that, the human's already
    been notified, so repeated not-confident replies just post the AI's
    suggestion without re-pinging and spamming the thread."""
    session = TICKET_SESSIONS.setdefault(
        int(thread_id), {"opener_id": None, "history": [], "ai_active": True, "escalated": False}
    )
    if session.get("escalated"):
        return False
    session["escalated"] = True
    return True


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
