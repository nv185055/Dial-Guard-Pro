import os
import logging
import random
import shutil
from tempfile import NamedTemporaryFile
from aiogram import types
import azure.cognitiveservices.speech as speechsdk
from bot_config import dp, AZURE_SPEECH_KEY, AZURE_SPEECH_REGION  # Import from bot_config
from database import Database  # Import your database handler
from pydub import AudioSegment
import aioftp  # Asynchronous FTP library
import aiofiles  # Asynchronous file handling

# Initialize your database connection
db = Database(
    host="localhost",
    port_id=5432,
    database="mydatabase",
    user="postgres",
    password="root"
)
db.connect()

# FTP server details
FTP_HOST = "198.199.80.152"
FTP_USER = "ftprishi@legendmkc.online"
FTP_PASS = "raJ7890@#R"

# Define the script creation process
async def start_script_creation(message: types.Message):
    # Generate a random number for the script ID
    random_number = random.randint(100000, 999999)
    script_id = f"legend{random_number}"

    # Save the script_id in the database
    script_db_id = db.insert_script(message.chat.id, script_id)
    if not script_db_id:
        await message.reply("⚠️ *Error:* Could not save script_id in the database. Please try again.", parse_mode=types.ParseMode.MARKDOWN)
        return

    logging.info(f"Script ID {script_id} created and saved in the database with ID {script_db_id}")

    # Ensure both script_id and part are saved in the state
    state_data = {"script_id": script_id, "part": 1}
    db.save_state(message.chat.id, state_data)
    logging.info(f"State saved for user {message.chat.id}: {state_data}")

    # Create a formatted message for the first part without sending script ID
    message_text = (
        f"📝 *Script Creation Started!*\n\n"
        f"👉 *Please enter the text for part 1 of your script.*\n\n"
        f"💬 *Suggested message:* 'Hello dear customer'\n"
        f"💡 *Tip:* Provide a clear introduction to the customer."
    )

    await message.reply(message_text, parse_mode=types.ParseMode.MARKDOWN)

async def handle_part(message: types.Message):
    # Retrieve the state data using custom state management
    state_data = db.get_state(message.chat.id)
    logging.info(f"State data retrieved for user {message.from_user.id}: {state_data}")

    # Ensure that state_data is a dictionary
    if not isinstance(state_data, dict):
        await message.reply("⚠️ *Error:* State data is not properly formatted. Please restart the script creation process.", parse_mode=types.ParseMode.MARKDOWN)
        return

    # Check if the state data contains the required keys
    if 'script_id' not in state_data or 'part' not in state_data:
        await message.reply("⚠️ *Error:* `script_id` or `part` not found in the state. Please restart the script creation process.", parse_mode=types.ParseMode.MARKDOWN)
        return

    part_number = state_data['part']
    file_url = await save_part(message, state_data['script_id'], part_number)

    # Define specific instructions for each part
    part_instructions = {
        1: "👉 *Please enter the text for part 2 of your script.*\n\n💬 *Suggested message:* 'Enter the verification code'\n🔐 *Tip:* This part should ask the customer for their verification code.",
        2: "👉 *Please enter the text for part 3 of your script.*\n\n💬 *Suggested message:* 'Sorry, you have entered the wrong code'\n⚠️ *Tip:* Use this part to notify the customer of an incorrect input.",
        3: "👉 *Please enter the text for part 4 of your script.*\n\n💬 *Suggested message:* 'Thank you, we will call you back'\n📞 *Tip:* Inform the customer that further communication will happen via call.",
        4: "👉 *Please enter the text for part 5 of your script.*\n\n💬 *Suggested message:* 'Please wait while we are working on that'\n⏳ *Tip:* Use this part to request the customer to wait while the process is ongoing.",
    }

    if part_number < 5:
        next_part = part_number + 1
        state_data['part'] = next_part
        db.save_state(message.chat.id, state_data)
        logging.info(f"State updated for user {message.chat.id}: {state_data}")
        await message.reply(part_instructions[part_number], parse_mode=types.ParseMode.MARKDOWN)
    else:
        # Final part has been completed, script creation is done
        await message.reply("✅ *Your script has been successfully created!* 🎉", parse_mode=types.ParseMode.MARKDOWN)

        # Send the final script creation message with the script ID
        script_id = state_data['script_id']
        message_text = (
            f"🎉 *Script Creation Completed!*\n\n"
            f"📜 *Script ID:* `{script_id}`\n\n"
            f"🎉 Your script is ready! You can now access all the parts you've created.\n\n"
            f"Thank you for using our service!"
        )

        await message.reply(message_text, parse_mode=types.ParseMode.MARKDOWN)

        # Clear the state after completion
        db.save_state(message.chat.id, {})

async def save_part(message: types.Message, script_id: str, part_number: int):
    # Define the base directory where scripts will be stored
    base_dir = "./scripts"  # The root directory for all scripts
    script_dir = os.path.join(base_dir, script_id)  # The specific directory for this script_id

    # Ensure the directory exists
    if not os.path.exists(script_dir):
        os.makedirs(script_dir)

    # Define the file name and path
    filename = f"part{part_number}.wav"
    filepath = os.path.join(script_dir, filename)

    # Get the user's preferred voice name
    user_id = message.from_user.id
    voice_name = db.get_voice_name(user_id) or "en-US-JennyNeural"  # Default to "en-US-JennyNeural" if not set

    # Convert text to speech and save to file
    await text_to_speech(message.text, filepath, voice_name)

    logging.info(f"Part {part_number} saved at {filepath} for script ID {script_id}")

    # Upload the file to the FTP server asynchronously
    await ftp_upload_async(filepath, f"{script_id}/{filename}")

    # Return the URL where the file can be accessed (if needed)
    file_url = f"http://localhost:8000/scripts/{script_id}/{filename}"  # Adjust to your server's base URL
    return file_url

async def text_to_speech(text: str, filepath: str, voice_name: str):
    try:
        # Azure Speech SDK Configuration
        speech_config = speechsdk.SpeechConfig(subscription=AZURE_SPEECH_KEY, region=AZURE_SPEECH_REGION)
        speech_config.speech_synthesis_voice_name = voice_name  # Set the voice name
        audio_config = speechsdk.audio.AudioOutputConfig(filename=filepath)

        synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)
        synthesizer.speak_text_async(text).get()

        # Load the saved audio file and resample to 8 kHz
        audio = AudioSegment.from_file(filepath)
        audio_8k = audio.set_frame_rate(8000)
        audio_8k.export(filepath, format="wav")

        return filepath
    except Exception as e:
        logging.error(f"Failed to convert text to speech: {e}")
        return None

async def ftp_upload_async(local_filepath, remote_filepath):
    """Asynchronous FTP upload using aioftp, with temp file handling."""
    try:
        # Create a temporary file to handle the asynchronous content reading and synchronous upload
        with NamedTemporaryFile(delete=False) as tmp_file:
            temp_filename = tmp_file.name  # Temporary file path

        # Asynchronously copy the file content into the temporary file
        async with aiofiles.open(local_filepath, 'rb') as source_file:
            async with aiofiles.open(temp_filename, 'wb') as temp_file:
                content = await source_file.read()  # Read content asynchronously
                await temp_file.write(content)  # Write content asynchronously to temporary file

        # Now perform FTP upload using aioftp with the temporary file path
        async with aioftp.Client.context(FTP_HOST, user=FTP_USER, password=FTP_PASS) as client:
            base_dir = '/scripts'
            
            # Navigate to the base directory, create if it does not exist
            try:
                await client.change_directory(base_dir)
            except aioftp.StatusCodeError:
                await client.make_directory(base_dir)
                await client.change_directory(base_dir)

            # Ensure the directory structure on the FTP server matches script_id
            dirs = remote_filepath.split('/')[:-1]  # Exclude the file name
            for dir in dirs:
                if dir:
                    try:
                        await client.change_directory(dir)
                    except aioftp.StatusCodeError:
                        await client.make_directory(dir)
                        await client.change_directory(dir)

            # Upload the temporary file using its path (sync operation)
            await client.upload(temp_filename, os.path.basename(remote_filepath))

            logging.info(f"File {local_filepath} uploaded to FTP as {remote_filepath}")
    except Exception as e:
        logging.error(f"Failed to upload file to FTP: {e}")
    finally:
        # Clean up the temporary file
        try:
            os.remove(temp_filename)
        except Exception as e:
            logging.error(f"Failed to delete temporary file {temp_filename}: {e}")

