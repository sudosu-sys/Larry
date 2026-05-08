import os
import asyncio
import asyncpg
import random
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google import genai
from google.genai import types

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")  # Your Neon connection string

ai_client = genai.Client(api_key=GEMINI_KEY).aio

SUDO_ID = 7706888177

# How many hours of silence = a fresh conversation window
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

config = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, temperature=0.85)

# Initialize FastAPI and Telegram App
app = FastAPI()
ptb_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()


# --- DATABASE LOGIC ---

async def fetch_history(chat_id: int):
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch(
        "SELECT role, content FROM chat_history WHERE user_id = $1 ORDER BY created_at ASC LIMIT 20",
        chat_id
    )
    await conn.close()
    history = []
    for row in rows:
        if not history or history[-1].role != row['role']:
            history.append(types.Content(role=row['role'], parts=[types.Part.from_text(text=row['content'])]))
        else:
            history[-1].parts.append(types.Part.from_text(text=row['content']))
    return history


async def save_message(chat_id: int, role: str, content: str):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute(
        "INSERT INTO chat_history (user_id, role, content) VALUES ($1, $2, $3)",
        chat_id, role, content
    )
    await conn.close()


async def get_session_state(chat_id: int):
    """
    Returns (last_message_at, sudo_replied_since, larry_routine_ran_at).
    Returns (None, False, None) if no session row exists yet.
    """
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow(
        "SELECT last_message_at, sudo_replied_since, larry_routine_ran_at FROM larry_sessions WHERE chat_id = $1",
        chat_id
    )
    await conn.close()
    if row:
        return row['last_message_at'], row['sudo_replied_since'], row['larry_routine_ran_at']
    return None, False, None


async def update_session_after_larry(chat_id: int):
    """Mark that Larry just ran the routine and update last_message_at."""
    now = datetime.now(timezone.utc)
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute(
        """
        INSERT INTO larry_sessions (chat_id, last_message_at, sudo_replied_since, larry_routine_ran_at)
        VALUES ($1, $2, false, $2)
        ON CONFLICT (chat_id) DO UPDATE
            SET last_message_at = $2,
                sudo_replied_since = false,
                larry_routine_ran_at = $2
        """,
        chat_id, now
    )
    await conn.close()


async def update_session_last_message(chat_id: int):
    """Update last_message_at without resetting anything else."""
    now = datetime.now(timezone.utc)
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute(
        """
        INSERT INTO larry_sessions (chat_id, last_message_at, sudo_replied_since, larry_routine_ran_at)
        VALUES ($1, $2, false, null)
        ON CONFLICT (chat_id) DO UPDATE
            SET last_message_at = $2
        """,
        chat_id, now
    )
    await conn.close()


async def mark_sudo_replied(chat_id: int):
    """Called when Sudo manually replies — flags next incoming message as a fresh window."""
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute(
        """
        INSERT INTO larry_sessions (chat_id, last_message_at, sudo_replied_since, larry_routine_ran_at)
        VALUES ($1, now(), true, null)
        ON CONFLICT (chat_id) DO UPDATE
            SET sudo_replied_since = true
        """,
        chat_id
    )
    await conn.close()


def is_fresh_window(last_message_at, sudo_replied_since: bool) -> bool:
    """Returns True if the 3-message routine should run."""
    # Sudo replied since last Larry interaction → always fresh
    if sudo_replied_since:
        return True
    # No prior session → fresh
    if last_message_at is None:
        return True
    # Time gap >= FRESH_WINDOW_HOURS → fresh
    now = datetime.now(timezone.utc)
    # Make sure last_message_at is timezone-aware
    if last_message_at.tzinfo is None:
        last_message_at = last_message_at.replace(tzinfo=timezone.utc)
    hours_elapsed = (now - last_message_at).total_seconds() / 3600
    return hours_elapsed >= FRESH_WINDOW_HOURS


# --- BOT LOGIC ---

async def run_checkin_routine(biz_msg, history, user_text: str):
    """
    Sends the 3-message check-in routine, then generates a natural
    follow-up reply that handles the actual content of the user's message.
    """
    # --- MESSAGE 1: Sudo is a busy CTO ---
    msg1_prompt = (
        f"The user just sent: \"{user_text}\"\n\n"
        "Send Message 1 of the check-in routine ONLY. "
        "Set the tone — Sudo is a very busy CTO, no apologies, just facts. "
        "1-2 sentences max. Do not include Message 2 or 3."
    )
    history_m1 = history + [types.Content(role='user', parts=[types.Part.from_text(text=msg1_prompt)])]
    resp1 = await ai_client.models.generate_content(
        model='gemini-2.5-flash', contents=history_m1, config=config
    )
    msg1_text = resp1.text.strip()
    await biz_msg.reply_text(msg1_text)
    await save_message(biz_msg.chat.id, 'model', msg1_text)

    # --- DELAY before message 2 ---
    await asyncio.sleep(random.uniform(2.0, 3.0))

    # --- MESSAGE 2: Going to check on Sudo ---
    msg2_prompt = (
        "Send Message 2 of the check-in routine ONLY. "
        "Tell them you are going to go check what Sudo is up to right now. "
        "1 sentence. Casual but purposeful. Do not include Message 1 or 3."
    )
    history_m2 = history_m1 + [
        types.Content(role='model', parts=[types.Part.from_text(text=msg1_text)]),
        types.Content(role='user', parts=[types.Part.from_text(text=msg2_prompt)])
    ]
    resp2 = await ai_client.models.generate_content(
        model='gemini-2.5-flash', contents=history_m2, config=config
    )
    msg2_text = resp2.text.strip()
    await biz_msg.reply_text(msg2_text)
    await save_message(biz_msg.chat.id, 'model', msg2_text)

    # --- DELAY before message 3 (longer — simulates actually going to check) ---
    await asyncio.sleep(random.uniform(3.0, 5.0))

    # --- MESSAGE 3: Status report + handle the actual message ---
    status_scenarios = [
        "deep in a coding session and completely locked in",
        "in a meeting and cannot step away right now",
        "on the move and cannot type at the moment"
    ]
    chosen_status = random.choice(status_scenarios)

    msg3_prompt = (
        f"Send Message 3 of the check-in routine ONLY. "
        f"You checked and Sudo is currently: {chosen_status}. "
        f"Report this back naturally in 1-2 sentences. "
        f"The user's original message was: \"{user_text}\". "
        f"If it contains a request, confirm you have logged it for Sudo. "
        f"Do not include Message 1 or 2."
    )
    history_m3 = history_m2 + [
        types.Content(role='model', parts=[types.Part.from_text(text=msg2_text)]),
        types.Content(role='user', parts=[types.Part.from_text(text=msg3_prompt)])
    ]
    resp3 = await ai_client.models.generate_content(
        model='gemini-2.5-flash', contents=history_m3, config=config
    )
    msg3_text = resp3.text.strip()
    await biz_msg.reply_text(msg3_text)
    await save_message(biz_msg.chat.id, 'model', msg3_text)


async def handle_business_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    biz_msg = update.business_message
    if not biz_msg or not biz_msg.text:
        return

    # If Sudo manually replied, flag it and do nothing else
    if biz_msg.from_user.id == SUDO_ID:
        await mark_sudo_replied(biz_msg.chat.id)
        return

    chat_id = biz_msg.chat.id
    user_text = biz_msg.text

    # Save the incoming user message
    await save_message(chat_id, 'user', user_text)

    # Pull session state to decide which mode to run
    last_message_at, sudo_replied_since, larry_routine_ran_at = await get_session_state(chat_id)
    fresh = is_fresh_window(last_message_at, sudo_replied_since)

    # Rebuild memory
    history = await fetch_history(chat_id)

    try:
        if fresh:
            # Run the full 3-message check-in routine
            await run_checkin_routine(biz_msg, history, user_text)
            await update_session_after_larry(chat_id)
        else:
            # Same session — Larry just responds naturally
            response = await ai_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=history,
                config=config
            )
            reply_text = response.text.strip()
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