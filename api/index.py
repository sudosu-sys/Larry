import os
import asyncio
import asyncpg
import random
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from groq import AsyncGroq

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

ai_client = AsyncGroq(api_key=GROQ_API_KEY)

SUDO_ID = 7706888177
FRESH_WINDOW_HOURS = 3

# --- SYSTEM PROMPT ---
SYSTEM_PROMPT = (
    "You are Larry, the AI chief of staff managing all incoming communications for Sudo — a CTO who moves fast and has no time to spare. "
    "You are sharp, efficient, and carry a quiet confidence. You don't apologize for Sudo's time, you explain it. "
    "\n\n"
    "CORE RULES:\n"
    "1. IDENTITY: Only introduce yourself as Larry on the very first message you ever send to a user. After that, never mention your name again.\n"
    "2. TONE: Minimalist and direct. No AI fluff, no filler phrases, no excessive politeness. You speak like someone who runs things.\n"
    "3. REQUESTS: If someone has a request for Sudo, confirm clearly that you have logged it and he will get to it when he surfaces.\n"
    "4. NATURAL FOLLOW-UPS: Once the 3-message routine has run in a session, just respond naturally and conversationally. "
    "Do not repeat the routine or the status check. Just handle whatever they say.\n"
    "\n\n"
    "THE 3-MESSAGE CHECK-IN ROUTINE:\n"
    "This runs at the start of every fresh conversation window. You send exactly 3 separate messages — NOT combined into one.\n"
    "\n"
    "Message 1 — sent immediately:\n"
    "Set the tone. Sudo is a very busy CTO. He does not sit around waiting to reply. "
    "Make it clear without being rude — he moves fast, his plate is full, and replies on his schedule. "
    "Keep it to 1-2 sentences max. Confident, not apologetic.\n"
    "\n"
    "Message 2 — sent ~2-3 seconds after Message 1:\n"
    "Tell them you are going to go check what Sudo is up to right now. "
    "Something like 'Hold on, let me go see what he's got going on.' — casual but purposeful. "
    "1 sentence. No more.\n"
    "\n"
    "Message 3 — sent ~3-5 seconds after Message 2:\n"
    "Come back with a status. Pick ONE of these scenarios randomly and make it feel real:\n"
    "  - He is deep in a coding session and completely locked in\n"
    "  - He is in a meeting and cannot step away\n"
    "  - He is on the move and cannot type right now\n"
    "Keep it to 1-2 sentences. If their original message was a request, add that you have logged it.\n"
    "\n\n"
    "IMPORTANT: The 3 messages must each be short, punchy, and feel like they came from a real person — not a chatbot doing a performance. "
    "No emojis unless the vibe clearly calls for it. No corporate speak."
)

app = FastAPI()
ptb_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()


# --- GROQ HELPER ---

async def groq_chat(messages: list, temperature: float = 0.85) -> str:
    response = await ai_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages,
        temperature=temperature,
        max_tokens=1024,
    )
    return response.choices[0].message.content.strip()


def build_groq_history(db_rows: list) -> list:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for role, content in db_rows:
        groq_role = "assistant" if role == "model" else "user"
        messages.append({"role": groq_role, "content": content})
    return messages


# --- DATABASE LOGIC ---

async def fetch_history(chat_id: int) -> list:
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch(
        "SELECT role, content FROM chat_history WHERE user_id = $1 ORDER BY created_at ASC LIMIT 20",
        chat_id
    )
    await conn.close()
    return build_groq_history([(row['role'], row['content']) for row in rows])


async def save_message(chat_id: int, role: str, content: str):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute(
        "INSERT INTO chat_history (user_id, role, content) VALUES ($1, $2, $3)",
        chat_id, role, content
    )
    await conn.close()


async def get_session_state(chat_id: int):
    """
    Returns (last_message_at, sudo_replied_since, larry_routine_ran_at, larry_active).
    Returns (None, False, None, True) if no session row exists yet.
    larry_active defaults to True so Larry handles brand new conversations.
    """
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow(
        "SELECT last_message_at, sudo_replied_since, larry_routine_ran_at, larry_active FROM larry_sessions WHERE chat_id = $1",
        chat_id
    )
    await conn.close()
    if row:
        return row['last_message_at'], row['sudo_replied_since'], row['larry_routine_ran_at'], row['larry_active']
    return None, False, None, True  # brand new chat — Larry should handle it


async def update_session_after_larry(chat_id: int):
    """Mark that Larry just ran the routine. larry_active = True."""
    now = datetime.now(timezone.utc)
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute(
        """
        INSERT INTO larry_sessions (chat_id, last_message_at, sudo_replied_since, larry_routine_ran_at, larry_active)
        VALUES ($1, $2, false, $2, true)
        ON CONFLICT (chat_id) DO UPDATE
            SET last_message_at = $2,
                sudo_replied_since = false,
                larry_routine_ran_at = $2,
                larry_active = true
        """,
        chat_id, now
    )
    await conn.close()


async def update_session_last_message(chat_id: int):
    """Update last_message_at without resetting anything else. larry_active stays as-is."""
    now = datetime.now(timezone.utc)
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute(
        """
        INSERT INTO larry_sessions (chat_id, last_message_at, sudo_replied_since, larry_routine_ran_at, larry_active)
        VALUES ($1, $2, false, null, true)
        ON CONFLICT (chat_id) DO UPDATE
            SET last_message_at = $2
        """,
        chat_id, now
    )
    await conn.close()


async def deactivate_larry(chat_id: int):
    """
    Called when Sudo replies. Silences Larry completely for this chat.
    Also sets sudo_replied_since = true so the NEXT inbound message
    triggers a fresh window if Sudo doesn't reply again in 3 hours.
    """
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute(
        """
        INSERT INTO larry_sessions (chat_id, last_message_at, sudo_replied_since, larry_routine_ran_at, larry_active)
        VALUES ($1, now(), true, null, false)
        ON CONFLICT (chat_id) DO UPDATE
            SET sudo_replied_since = true,
                larry_active = false,
                last_message_at = now()
        """,
        chat_id
    )
    await conn.close()


def is_fresh_window(last_message_at, sudo_replied_since: bool) -> bool:
    if sudo_replied_since:
        return True
    if last_message_at is None:
        return True
    now = datetime.now(timezone.utc)
    if last_message_at.tzinfo is None:
        last_message_at = last_message_at.replace(tzinfo=timezone.utc)
    hours_elapsed = (now - last_message_at).total_seconds() / 3600
    return hours_elapsed >= FRESH_WINDOW_HOURS


# --- BOT LOGIC ---

async def run_checkin_routine(biz_msg, history: list, user_text: str):
    # --- MESSAGE 1 ---
    messages_m1 = history + [{
        "role": "user",
        "content": (
            f"The user just sent: \"{user_text}\"\n\n"
            "Send Message 1 of the check-in routine ONLY. "
            "Set the tone — Sudo is a very busy CTO, no apologies, just facts. "
            "1-2 sentences max. Do not include Message 2 or 3."
        )
    }]
    msg1_text = await groq_chat(messages_m1)
    await biz_msg.reply_text(msg1_text)
    await save_message(biz_msg.chat.id, 'model', msg1_text)

    await asyncio.sleep(random.uniform(2.0, 3.0))

    # --- MESSAGE 2 ---
    messages_m2 = messages_m1 + [
        {"role": "assistant", "content": msg1_text},
        {
            "role": "user",
            "content": (
                "Send Message 2 of the check-in routine ONLY. "
                "Tell them you are going to go check what Sudo is up to right now. "
                "1 sentence. Casual but purposeful. Do not include Message 1 or 3."
            )
        }
    ]
    msg2_text = await groq_chat(messages_m2)
    await biz_msg.reply_text(msg2_text)
    await save_message(biz_msg.chat.id, 'model', msg2_text)

    await asyncio.sleep(random.uniform(3.0, 5.0))

    # --- MESSAGE 3 ---
    status_scenarios = [
        "deep in a coding session and completely locked in",
        "in a meeting and cannot step away right now",
        "on the move and cannot type at the moment"
    ]
    chosen_status = random.choice(status_scenarios)

    messages_m3 = messages_m2 + [
        {"role": "assistant", "content": msg2_text},
        {
            "role": "user",
            "content": (
                f"Send Message 3 of the check-in routine ONLY. "
                f"You checked and Sudo is currently: {chosen_status}. "
                f"Report this back naturally in 1-2 sentences. "
                f"The user's original message was: \"{user_text}\". "
                f"If it contains a request, confirm you have logged it for Sudo. "
                f"Do not include Message 1 or 2."
            )
        }
    ]
    msg3_text = await groq_chat(messages_m3)
    await biz_msg.reply_text(msg3_text)
    await save_message(biz_msg.chat.id, 'model', msg3_text)


async def handle_business_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    biz_msg = update.business_message
    if not biz_msg or not biz_msg.text:
        return

    chat_id = biz_msg.chat.id

    # Sudo replied — silence Larry and hand the conversation back
    if biz_msg.from_user.id == SUDO_ID:
        await deactivate_larry(chat_id)
        return

    user_text = biz_msg.text

    # Pull session state
    last_message_at, sudo_replied_since, larry_routine_ran_at, larry_active = await get_session_state(chat_id)

    # ✅ THE KEY GATE: if Larry has been deactivated, do nothing
    if not larry_active:
        # Still log the message so history stays intact, but don't reply
        await save_message(chat_id, 'user', user_text)
        await update_session_last_message(chat_id)
        return

    # Larry is active — save message and proceed
    await save_message(chat_id, 'user', user_text)

    fresh = is_fresh_window(last_message_at, sudo_replied_since)
    history = await fetch_history(chat_id)

    try:
        if fresh:
            await run_checkin_routine(biz_msg, history, user_text)
            await update_session_after_larry(chat_id)
        else:
            reply_text = await groq_chat(history)
            await save_message(chat_id, 'model', reply_text)
            await biz_msg.reply_text(reply_text)
            await update_session_last_message(chat_id)

    except Exception as e:
        print(f"Error generating AI response: {e}")


ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_business_chat))


# --- WEBHOOK ENDPOINT ---

@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    if not ptb_app._initialized:
        await ptb_app.initialize()
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return Response(status_code=200)