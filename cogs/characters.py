import discord
from discord.ext import commands
from discord import Option

import database as db


class CharacterCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    character = discord.SlashCommandGroup("character", "Manage player <-> character mappings")

    @character.command(name="link", description="Link a Discord user to their D&D character name")
    async def link(
        self,
        ctx: discord.ApplicationContext,
        user: Option(discord.Member, "The player"),
        character_name: Option(str, "Their character's name"),
    ):
        await db.link_character(ctx.guild.id, user.id, user.display_name, character_name)
        await ctx.respond(f"Got it — **{user.display_name}** plays **{character_name}**.")

    @character.command(name="list", description="Show current player <-> character mappings")
    async def list_characters(self, ctx: discord.ApplicationContext):
        characters = await db.get_characters(ctx.guild.id)
        if not characters:
            await ctx.respond("No characters linked yet. Use `/character link` to add some.")
            return
        lines = [f"• **{c['discord_name']}** plays **{c['character_name']}**" for c in characters]
        await ctx.respond("\n".join(lines))

    @discord.slash_command(name="summary-channel-set", description="Set where session recaps get posted")
    @commands.has_permissions(manage_guild=True)
    async def set_summary_channel(
        self,
        ctx: discord.ApplicationContext,
        channel: Option(discord.TextChannel, "Channel for recaps"),
    ):
        await db.set_summary_channel(ctx.guild.id, channel.id)
        await ctx.respond(f"Session recaps will now be posted in {channel.mention}.")


def setup(bot):
    bot.add_cog(CharacterCog(bot))
