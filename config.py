import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "small")
DATA_DIR = os.getenv("DATA_DIR", "./data")
DB_PATH = os.path.join(DATA_DIR, "campaign.db")
RECORDINGS_DIR = os.path.join(DATA_DIR, "recordings")

os.makedirs(RECORDINGS_DIR, exist_ok=True)
