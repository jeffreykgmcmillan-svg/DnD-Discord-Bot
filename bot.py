import discord
from discord.ext import commands

import config
import database as db

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True

bot = discord.Bot(intents=intents)

COGS = ["cogs.session", "cogs.characters", "cogs.notes"]


@bot.event
async def on_ready():
    await db.init_db()
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print("Slash commands are registered per-guild almost instantly with py-cord.")


for cog in COGS:
    bot.load_extension(cog)


if __name__ == "__main__":
    bot.run(config.DISCORD_TOKEN)
