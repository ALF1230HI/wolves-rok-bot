"""AI-generated first response for support tickets.

Shared by both bot.py (the live Discord bot) and webapp.py (the donation
website's "Get Help" flow) so a ticket gets the same automated help
whichever way it was opened. Uses Google's Gemini API directly over aiohttp
(already a dependency everywhere else in this project) rather than adding
the official SDK as a new dependency. Gemini's free tier requires no
credit card and no billing, so this runs at zero ongoing cost.

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

Given the member's issue below, write a short, friendly, specific reply (3-5 sentences, \
plain text, no markdown headers) that actually tries to solve it using the above. Then \
decide: can a member realistically fix this themselves from your reply, or does this need a \
human (e.g. a bug, a spreadsheet/permissions problem, or anything you don't have enough \
information to solve confidently)?

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


async def get_ai_response(issue: str) -> dict:
    """Returns {"reply": str, "confident": bool}."""
    fallback = {
        "reply": (
            "Thanks for the details! I wasn't able to put together a suggestion for this one "
            "— looping in a human to help."
        ),
        "confident": False,
    }
    if not GEMINI_API_KEY:
        print("[ai_helper] no GEMINI_API_KEY set, skipping AI response")
        return fallback

    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": issue}]}],
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
                    return fallback
                data = await resp.json()
        content = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(content)
        reply = str(parsed.get("reply", "")).strip()
        confident = bool(parsed.get("confident", False))
        if not reply:
            return fallback
        return {"reply": reply, "confident": confident}
    except Exception as e:
        print(f"[ai_helper] error getting AI response: {e!r}")
        return fallback


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

