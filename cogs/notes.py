import discord
from discord.ext import commands
from discord import Option

import database as db


class NotesCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    notes = discord.SlashCommandGroup("notes", "Browse past session notes")

    @notes.command(name="recent", description="Show recent session recaps")
    async def recent(
        self,
        ctx: discord.ApplicationContext,
        count: Option(int, "How many sessions back", default=3),
    ):
        sessions = await db.get_recent_sessions(ctx.guild.id, limit=count)
        if not sessions:
            await ctx.respond("No past sessions logged yet.")
            return
        await ctx.defer()
        for s in sessions:
            date = s["ended_at"][:10] if s["ended_at"] else "unknown date"
            summary = s["summary_text"] or "(no summary)"
            await ctx.followup.send(f"**Session #{s['id']} — {date}**\n{summary[:1800]}")

    @notes.command(name="search", description="Search past session recaps for a keyword or topic")
    async def search(
        self,
        ctx: discord.ApplicationContext,
        term: Option(str, "Keyword to search for"),
    ):
        results = await db.search_sessions(ctx.guild.id, term)
        if not results:
            await ctx.respond(f"No past sessions mention '{term}'.")
            return
        await ctx.defer()
        lines = [f"• Session #{s['id']} ({s['ended_at'][:10] if s['ended_at'] else '?'})" for s in results]
        await ctx.followup.send(f"Found '{term}' in:\n" + "\n".join(lines))


def setup(bot):
    bot.add_cog(NotesCog(bot))
