import logging

import discord
from discord.ext import commands
from discord import Option

import database as db

logger = logging.getLogger("characters")


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
        logger.info(f"/character link invoked by {ctx.author.display_name}: {user.display_name} -> {character_name}")
        await db.link_character(ctx.guild.id, user.id, user.display_name, character_name)
        await ctx.respond(f"Got it — **{user.display_name}** plays **{character_name}**.")

    @character.command(name="set-dm", description="Mark a user as the Dungeon Master")
    async def set_dm(
        self,
        ctx: discord.ApplicationContext,
        user: Option(discord.Member, "The DM"),
        title: Option(str, "Optional label for them in recaps", default="Dungeon Master"),
    ):
        logger.info(f"/character set-dm invoked by {ctx.author.display_name}: {user.display_name} as '{title}'")
        await db.set_dm(ctx.guild.id, user.id, user.display_name, title)
        await ctx.respond(f"Got it — **{user.display_name}** is now marked as the **{title}**.")

    @character.command(name="list", description="Show current player <-> character mappings")
    async def list_characters(self, ctx: discord.ApplicationContext):
        logger.info(f"/character list invoked by {ctx.author.display_name}")
        characters = await db.get_characters(ctx.guild.id)
        if not characters:
            await ctx.respond("No characters linked yet. Use `/character link` to add some.")
            return
        lines = []
        for c in characters:
            if c.get("is_dm"):
                lines.append(f"• 🎲 **{c['discord_name']}** is the **{c['character_name']}**")
            else:
                lines.append(f"• **{c['discord_name']}** plays **{c['character_name']}**")
        await ctx.respond("\n".join(lines))

    @discord.slash_command(name="summary-channel-set", description="Set where session recaps get posted")
    @commands.has_permissions(manage_guild=True)
    async def set_summary_channel(
        self,
        ctx: discord.ApplicationContext,
        channel: Option(discord.TextChannel, "Channel for recaps"),
    ):
        logger.info(f"/summary-channel-set invoked by {ctx.author.display_name}: #{channel.name}")
        await db.set_summary_channel(ctx.guild.id, channel.id)
        await ctx.respond(f"Session recaps will now be posted in {channel.mention}.")


def setup(bot):
    bot.add_cog(CharacterCog(bot))
