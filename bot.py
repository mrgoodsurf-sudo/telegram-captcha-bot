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
POSTED_USERS_FILE = "posted_users.json"

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
    
    blacklist = set()
    now = datetime.now()
    
    with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                user_id = int(row['user_id'])
                expires = row.get('expires', 'PERMANENT').strip().upper()
                
                # Check if ban is still active
                if expires == 'PERMANENT':
                    blacklist.add(user_id)
                else:
                    # Parse expiration date
                    try:
                        # Handle both formats: "YYYY-MM-DD HH:MM:SS" and "YYYY-MM-DD"
                        if ' ' in expires:
                            expire_date = datetime.strptime(expires, '%Y-%m-%d %H:%M:%S')
                        elif 'T' in expires:
                            expire_date = datetime.strptime(expires, '%Y-%m-%dT%H:%M:%S')
                        else:
                            expire_date = datetime.strptime(expires, '%Y-%m-%d')
                        
                        # Only add if ban hasn't expired yet
                        if expire_date > now:
                            blacklist.add(user_id)
                    except ValueError:
                        # If date format is invalid, treat as permanent for safety
                        logger.warning(f"Invalid expires format for user {user_id}: {expires}. Treating as permanent.")
                        blacklist.add(user_id)
            except (ValueError, KeyError) as e:
                logger.error(f"Error reading blacklist row: {e}")
                continue
    
    return blacklist

# Check CAS API
async def check_cas_ban(user_id: int) -> bool:
    """
    Check if user is banned in CAS (Combot Anti-Spam) database.
    Returns True if banned, False otherwise.
    """
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.cas.chat/check?user_id={user_id}",
                timeout=aiohttp.ClientTimeout(total=3)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    is_banned = data.get('ok', False)
                    if is_banned:
                        logger.info(f"User {user_id} found in CAS database")
                    return is_banned
                else:
                    logger.warning(f"CAS API returned status {response.status}")
                    return False
    except asyncio.TimeoutError:
        logger.warning(f"CAS API timeout for user {user_id}")
        return False
    except Exception as e:
        logger.error(f"CAS API error for user {user_id}: {e}")
        return False

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

# Load posted users
def load_posted_users():
    if not os.path.exists(POSTED_USERS_FILE):
        return []
    with open(POSTED_USERS_FILE, 'r') as f:
        return json.load(f)

# Save posted users
def save_posted_users(users):
    with open(POSTED_USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)

# Check first message for spam
async def check_first_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    
    user_id = update.message.from_user.id
    posted_users = load_posted_users()
    
    if user_id in posted_users:
        return
    
    config = load_config()
    spam_patterns = config.get('spam_patterns', [])
    message_text = update.message.text
    message_lower = message_text.lower()
    
    # Check for multiple Telegram links
    import re
    telegram_links = re.findall(r't\.me/[^\s]+', message_lower)
    if len(telegram_links) >= 2:
        chat_id = update.effective_chat.id
        logger.warning(f"Multiple Telegram links detected from user {user_id} ({len(telegram_links)} links)")
        try:
            await update.message.delete()
            await context.bot.ban_chat_member(chat_id, user_id, until_date=datetime.now() + timedelta(hours=24))
        except Exception as e:
            logger.error(f"Failed to ban multi-link spammer {user_id}: {e}")
        return
    
    # Check spam patterns
    for pattern in spam_patterns:
        if pattern.lower() in message_lower:
            chat_id = update.effective_chat.id
            logger.warning(f"Spam detected from user {user_id}: pattern '{pattern}'")
            try:
                await update.message.delete()
                await context.bot.ban_chat_member(chat_id, user_id, until_date=datetime.now() + timedelta(hours=24))
            except Exception as e:
                logger.error(f"Failed to ban spammer {user_id}: {e}")
            return
    
    posted_users.append(user_id)
    save_posted_users(posted_users)

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
    
    # Check CAS database
    is_cas_banned = await check_cas_ban(user_id)
    if is_cas_banned:
        logger.info(f"CAS-banned user {user_id} attempted to join. Banning permanently.")
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
    
    # Create adaptive keyboard based on number of options
    buttons = []
    options = config['button_options']
    num_options = len(options)
    
    # Determine grid layout
    if num_options <= 3:
        # Single row
        buttons = [[InlineKeyboardButton(opt, callback_data=f"captcha_{user_id}_{opt}") for opt in options]]
    elif num_options <= 6:
        # 2 rows
        cols = 3 if num_options > 4 else 2
        for i in range(0, num_options, cols):
            row = [InlineKeyboardButton(opt, callback_data=f"captcha_{user_id}_{opt}") for opt in options[i:i+cols]]
            buttons.append(row)
    else:
        # 3+ rows, 3 columns
        for i in range(0, num_options, 3):
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
    
    # Schedule timeout kick (2 minutes)
    context.job_queue.run_once(
        timeout_kick,
        120,
        data={'chat_id': chat_id, 'user_id': user_id},
        name=f"timeout_{user_id}"
    )

# Handle captcha response
async def captcha_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    # Immediate feedback to user while processing
    await query.answer("⏳ Vérification en cours...", show_alert=False)
    
    # Parse callback data
    _, target_user_id, answer = query.data.split('_', 2)  # Use maxsplit=2 to handle answers with underscores
    target_user_id = int(target_user_id)
    
    # Only allow the target user to answer
    if query.from_user.id != target_user_id:
        return  # Silently ignore, already answered
    
    chat_id = update.effective_chat.id
    config = load_config()
    attempts = load_attempts()
    
    user_key = str(target_user_id)
    if user_key not in attempts:
        return
    
    # ALL answers are valid - unmute user
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
    
    # Check if this was the "best" answer for fun message
    if 'best_answer' in config and answer == config['best_answer'] and 'fun_message' in config:
        # Send fun message
        user = query.from_user
        username = f"@{user.username}" if user.username else user.first_name
        fun_text = config['fun_message'].replace('{username}', username).replace('{user}', username)
        
        try:
            fun_msg = await context.bot.send_message(chat_id, fun_text)
            # Schedule deletion in background (non-blocking)
            asyncio.create_task(delete_fun_message_later(context.bot, chat_id, fun_msg.message_id))
        except Exception as e:
            logger.error(f"Failed to send fun message: {e}")
    
    # Remove from attempts and cancel timeout
    del attempts[user_key]
    save_attempts(attempts)
    
    # Cancel timeout job
    jobs = context.job_queue.get_jobs_by_name(f"timeout_{target_user_id}")
    for job in jobs:
        job.schedule_removal()
    
    logger.info(f"User {target_user_id} passed captcha with answer: {answer}")

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

# Background task to delete fun message after 48 hours
async def delete_fun_message_later(bot, chat_id, message_id):
    """Delete a message after 48 hours without blocking the main handler"""
    await asyncio.sleep(172800)  # 48 hours
    try:
        await bot.delete_message(chat_id, message_id)
        logger.info(f"Deleted fun message {message_id} after 48 hours")
    except Exception as e:
        logger.error(f"Failed to delete fun message after 48h: {e}")

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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_first_message))
    application.add_handler(MessageHandler(filters.StatusUpdate.ALL, delete_service_messages))
    
    logger.info("Bot Damoclès démarré.")
    application.run_polling(allowed_updates=['chat_member', 'callback_query', 'message'])

if __name__ == '__main__':
    while True:
        try:
            main()
        except KeyboardInterrupt:
            logger.info("Bot stopped manually (Ctrl+C)")
            break
        except Exception as e:
            logger.error(f"Bot crashed with error: {e}")
            logger.error("Restarting in 10 seconds...")
            import time
            time.sleep(10)
            logger.info("Attempting restart...")
