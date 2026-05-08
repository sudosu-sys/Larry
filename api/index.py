import os
import asyncpg
from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google import genai
from google.genai import types

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL") # Your Neon connection string

ai_client = genai.Client(api_key=GEMINI_KEY).aio

SYSTEM_PROMPT = (
    "You are Larry, the AI assistant managing communications for Sudo, a CTO. "
    "Protocol: "
    "1. Identity: Only introduce yourself as Larry in your very first message to a user. "
    "2. Status: In the initial interaction, state that Sudo is a busy CTO and cannot reply instantly. "
    "3. Context: Once the conversation has started, do NOT repeat your name or the 'Sudo is busy' disclaimer. "
    "Just respond naturally to the follow-up questions. "
    "4. Tone: Minimalist, professional, and functional. No AI fluff. "
    "5. Action: If a message is a request for Sudo, confirm you have logged it for his review."
)

config = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, temperature=0.7)

# Initialize FastAPI and Telegram App
app = FastAPI()
ptb_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# --- DATABASE LOGIC ---
async def fetch_history(user_id: int):
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch(
        "SELECT role, content FROM chat_history WHERE user_id = $1 ORDER BY created_at ASC LIMIT 20", 
        user_id
    )
    await conn.close()
    
    history = []
    for row in rows:
        # Group consecutive messages of the same role to prevent Gemini API crashes
        if not history or history[-1].role != row['role']:
            # Fixed TypeError by explicitly declaring 'text='
            history.append(types.Content(role=row['role'], parts=[types.Part.from_text(text=row['content'])]))
        else:
            history[-1].parts.append(types.Part.from_text(text=row['content']))
            
    return history

async def save_message(user_id: int, role: str, content: str):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute(
        "INSERT INTO chat_history (user_id, role, content) VALUES ($1, $2, $3)",
        user_id, role, content
    )
    await conn.close()

# --- BOT LOGIC ---
async def handle_business_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    biz_msg = update.business_message
    if not biz_msg or not biz_msg.text:
        return

    # Your Telegram ID based on the Neon DB dump
    SUDO_ID = 7706888177
    
    # If you step in and manually reply, Larry ignores the message 
    # so he doesn't talk to you or log your replies as user prompts.
    if biz_msg.from_user.id == SUDO_ID:
        return

    # Group the memory by the Chat ID, not the sender's ID
    chat_id = biz_msg.chat.id
    user_text = biz_msg.text

    # 1. Save incoming message
    await save_message(chat_id, 'user', user_text)

    # 2. Rebuild memory from Neon
    history = await fetch_history(chat_id)

    # 3. Generate response with context
    try:
        response = await ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=history,
            config=config
        )
        reply_text = response.text
        
        # 4. Save response to Neon and send
        await save_message(chat_id, 'model', reply_text)
        await biz_msg.reply_text(reply_text)
        
    except Exception as e:
        print(f"Error generating AI response: {e}")

ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_business_chat))

# --- WEBHOOK ENDPOINT ---
@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    # Initialize the PTB application if it isn't already
    if not ptb_app._initialized:
        await ptb_app.initialize()

    # Parse the incoming JSON from Telegram into an Update object
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    
    # Process the update statelessly
    await ptb_app.process_update(update)
    
    return Response(status_code=200)