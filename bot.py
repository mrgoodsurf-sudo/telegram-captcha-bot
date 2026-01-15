import os
import json
import csv
import asyncio
import logging
from datetime import datetime, timedelta
from threading import Thread
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ChatMemberHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ChatMemberStatus

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Files
CONFIG_FILE = "config.json"
ATTEMPTS_FILE = "attempts.json"
BLACKLIST_FILE = "blacklist.csv"

# Flood control
CAPTCHA_DELAY = 3  # seconds between each captcha
MAX_PENDING_CAPTCHAS = 20  # maximum simultaneous captchas
last_captcha_time = 0

# Bot start time (to ignore old events on restart)
bot_start_time = datetime.now()

# Flask keep-alive server
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot Damoclès is running!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    thread = Thread(target=run_flask)
    thread.daemon = True
    thread.start()

# Load config
def load_config():
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

# Load blacklist
def load_blacklist():
    if not os.path.exists(BLACKLIST_FILE):
        return set()
    with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return {int(row['user_id']) for row in reader}

# Load attempts
def load_attempts():
    if not os.path.exists(ATTEMPTS_FILE):
        return {}
    with open(ATTEMPTS_FILE, 'r') as f:
        return json.load(f)

# Save attempts
def save_attempts(attempts):
    with open(ATTEMPTS_FILE, 'w') as f:
        json.dump(attempts, f, indent=2)

# Handle new member with flood control
async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_captcha_time
    
    chat_member = update.chat_member
    
    # Only handle new members joining
    if chat_member.new_chat_member.status not in [ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED]:
        return
    if chat_member.old_chat_member.status != ChatMemberStatus.LEFT:
        return
    
    user = chat_member.new_chat_member.user
    chat_id = update.effective_chat.id
    user_id = user.id
    
    # Ignore old events (from before bot restart) to avoid retroactive captchas
    # Check if update has a date (it should always have one)
    if update.chat_member.date:
        event_time = update.chat_member.date
        # Make datetime timezone-aware for comparison (use event's timezone)
        time_since_event = datetime.now(event_time.tzinfo) - event_time
        
        # Ignore events older than 2 minutes
        if time_since_event.total_seconds() > 120:
            logger.info(f"Ignoring old join event for user {user_id} ({time_since_event.total_seconds():.0f}s old)")
            return
    
    # Check blacklist
    blacklist = load_blacklist()
    if user_id in blacklist:
        logger.info(f"Blacklisted user {user_id} attempted to join. Banning permanently.")
        await context.bot.ban_chat_member(chat_id, user_id, until_date=datetime.now() + timedelta(days=365))
        return
    
    # Flood control: if too many pending captchas, temporary kick
    attempts = load_attempts()
    if len(attempts) >= MAX_PENDING_CAPTCHAS:
        logger.warning(f"Flood mode: {len(attempts)} pending captchas. Temporary 1min kick for user {user_id}.")
        try:
            username_display = f"@{user.username}" if user.username else user.first_name
            message = await context.bot.send_message(
                chat_id,
                f"⏳ Trop de demandes simultanées. {username_display}, réessayez dans 1 minute."
            )
            # Delete message after 5 seconds
            await asyncio.sleep(5)
            await context.bot.delete_message(chat_id, message.message_id)
        except Exception as e:
            logger.error(f"Failed to send flood message: {e}")
        
        await context.bot.ban_chat_member(
            chat_id,
            user_id,
            until_date=datetime.now() + timedelta(minutes=1)
        )
        return
    
    # Restrict user immediately
    await context.bot.restrict_chat_member(
        chat_id,
        user_id,
        permissions={'can_send_messages': False}
    )
    
    # Flood control: wait between captchas to avoid Telegram rate limits
    current_time = asyncio.get_event_loop().time()
    time_since_last = current_time - last_captcha_time
    if time_since_last < CAPTCHA_DELAY:
        await asyncio.sleep(CAPTCHA_DELAY - time_since_last)
    
    last_captcha_time = asyncio.get_event_loop().time()
    
    # Load config
    config = load_config()
    
    # Create keyboard (3x3 grid)
    buttons = []
    options = config['button_options']
    for i in range(0, len(options), 3):
        row = [InlineKeyboardButton(opt, callback_data=f"captcha_{user_id}_{opt}") for opt in options[i:i+3]]
        buttons.append(row)
    
    keyboard = InlineKeyboardMarkup(buttons)
    
    # Send captcha
    try:
        with open(config['image_path'], 'rb') as photo:
            message = await context.bot.send_photo(
                chat_id,
                photo,
                caption=config['welcome_message'],
                reply_markup=keyboard
            )
    except Exception as e:
        logger.error(f"Failed to send captcha to {user_id}: {e}")
        # If captcha fails to send, kick the user for safety
        await context.bot.ban_chat_member(
            chat_id,
            user_id,
            until_date=datetime.now() + timedelta(hours=24)
        )
        return
    
    # Store attempt data
    attempts[str(user_id)] = {
        'tries': 0,
        'message_id': message.message_id,
        'join_time': datetime.now().isoformat()
    }
    save_attempts(attempts)
    
    # Schedule timeout kick (1 minute)
    context.job_queue.run_once(
        timeout_kick,
        60,
        data={'chat_id': chat_id, 'user_id': user_id},
        name=f"timeout_{user_id}"
    )

# Handle captcha response
async def captcha_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # Parse callback data
    _, target_user_id, answer = query.data.split('_')
    target_user_id = int(target_user_id)
    
    # Only allow the target user to answer
    if query.from_user.id != target_user_id:
        await query.answer("❌ Ce captcha n'est pas pour toi.", show_alert=True)
        return
    
    chat_id = update.effective_chat.id
    config = load_config()
    attempts = load_attempts()
    
    user_key = str(target_user_id)
    if user_key not in attempts:
        return
    
    # Check answer
    if answer == config['correct_answer']:
        # Correct! Unmute user
        await context.bot.restrict_chat_member(
            chat_id,
            target_user_id,
            permissions={
                'can_send_messages': True,
                'can_send_media_messages': True,
                'can_send_polls': True,
                'can_send_other_messages': True,
                'can_add_web_page_previews': True,
                'can_invite_users': True,
                'can_pin_messages': True
            }
        )
        
        # Delete captcha message
        try:
            await context.bot.delete_message(chat_id, attempts[user_key]['message_id'])
        except Exception as e:
            logger.error(f"Failed to delete captcha message: {e}")
        
        # Remove from attempts and cancel timeout
        del attempts[user_key]
        save_attempts(attempts)
        
        # Cancel timeout job
        jobs = context.job_queue.get_jobs_by_name(f"timeout_{target_user_id}")
        for job in jobs:
            job.schedule_removal()
        
        logger.info(f"User {target_user_id} passed captcha.")
    else:
        # Wrong answer
        attempts[user_key]['tries'] += 1
        tries_left = 3 - attempts[user_key]['tries']
        
        if tries_left > 0:
            await query.answer(f"❌ Mauvaise réponse. Il te reste {tries_left} tentative(s).", show_alert=True)
            save_attempts(attempts)
        else:
            # Out of tries - kick and ban 24h
            try:
                await context.bot.delete_message(chat_id, attempts[user_key]['message_id'])
            except Exception as e:
                logger.error(f"Failed to delete captcha message: {e}")
            
            await context.bot.ban_chat_member(
                chat_id,
                target_user_id,
                until_date=datetime.now() + timedelta(hours=24)
            )
            
            del attempts[user_key]
            save_attempts(attempts)
            
            # Cancel timeout job
            jobs = context.job_queue.get_jobs_by_name(f"timeout_{target_user_id}")
            for job in jobs:
                job.schedule_removal()
            
            logger.info(f"User {target_user_id} failed captcha 3 times. Banned for 24h.")

# Timeout kick after 1 minute
async def timeout_kick(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    chat_id = data['chat_id']
    user_id = data['user_id']
    
    attempts = load_attempts()
    user_key = str(user_id)
    
    if user_key in attempts:
        # Still hasn't answered - kick and ban 24h
        try:
            await context.bot.delete_message(chat_id, attempts[user_key]['message_id'])
        except Exception as e:
            logger.error(f"Failed to delete timeout captcha message: {e}")
        
        await context.bot.ban_chat_member(
            chat_id,
            user_id,
            until_date=datetime.now() + timedelta(hours=24)
        )
        
        del attempts[user_key]
        save_attempts(attempts)
        
        logger.info(f"User {user_id} timed out (1min). Banned for 24h.")

# Delete service messages
async def delete_service_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception as e:
        logger.error(f"Failed to delete service message: {e}")

def main():
    token = os.environ.get('TELEGRAM_TOKEN')
    if not token:
        raise ValueError("TELEGRAM_TOKEN environment variable not set")
    
    # Start keep-alive server
    keep_alive()
    logger.info("Keep-alive server started on port 8080")
    
    application = Application.builder().token(token).build()
    
    # Handlers
    application.add_handler(ChatMemberHandler(new_member_handler, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(CallbackQueryHandler(captcha_callback, pattern=r'^captcha_'))
    application.add_handler(MessageHandler(filters.StatusUpdate.ALL, delete_service_messages))
    
    logger.info("Bot Damoclès démarré.")
    application.run_polling(allowed_updates=['chat_member', 'callback_query', 'message'])

if __name__ == '__main__':
    main()
