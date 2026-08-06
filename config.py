import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "small")
DATA_DIR = os.getenv("DATA_DIR", "./data")
DB_PATH = os.path.join(DATA_DIR, "campaign.db")
RECORDINGS_DIR = os.path.join(DATA_DIR, "recordings")

# Optional: your server's ID, so slash commands sync instantly to it instead
# of relying on Discord's global command sync (which can take up to an hour
# to propagate new/changed commands). Leave unset to sync globally instead.
_guild_id = os.getenv("DISCORD_GUILD_ID")
GUILD_IDS = [int(_guild_id)] if _guild_id else None

os.makedirs(RECORDINGS_DIR, exist_ok=True)
