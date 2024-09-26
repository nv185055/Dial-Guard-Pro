from aiogram import Bot, Dispatcher
from aiogram.contrib.fsm_storage.memory import MemoryStorage
import os

# Load sensitive values from environment variables
TOKEN = os.getenv('TELEGRAM_TOKEN', '')
AZURE_SPEECH_KEY = os.getenv('AZURE_SPEECH_KEY', '')
AZURE_SPEECH_REGION = os.getenv('AZURE_SPEECH_REGION', '')

# Telegram bot setup
bot = Bot(token=TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())
