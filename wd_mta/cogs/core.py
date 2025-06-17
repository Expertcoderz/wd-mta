import logging
from datetime import UTC, datetime, timedelta

import discord
from discord.ext import commands
from typeguard import typechecked

from ..discord import InfoEmbed, WDMTABot

logger = logging.getLogger(__name__)


@typechecked
def format_timedelta(delta: timedelta) -> str:
    days, rem = divmod(delta.total_seconds(), 86_400)
    hours, rem = divmod(rem, 3600)
    mins, rem = divmod(rem, 60)

    if days > 0:
        return f"{days:.0f} d {hours:.0f} h {mins:.0f} min {rem:.0f} s"

    if hours > 0:
        return f"{hours:.0f} h {mins:.0f} min {rem:.0f} s"

    if mins > 0:
        return f"{mins:.0f} min {rem:.0f} s"

    return f"{rem:.0f} s"


@typechecked
class CoreCog(commands.Cog):
    def __init__(self, bot: WDMTABot, /):
        self.bot = bot

        self._start_time = datetime.now(UTC)

    @discord.app_commands.command(name="ping", description="Displays the bot's ping.")
    async def _ping(self, interaction: discord.Interaction):
        now = datetime.now(UTC)

        await interaction.response.send_message(
            embed=InfoEmbed(
                title="Pong!",
                description=f"{(now - interaction.created_at).total_seconds() * 1000:.0f} ms",
            )
        )

    @discord.app_commands.command(
        name="uptime", description="Displays the bot's uptime information."
    )
    async def _uptime(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=InfoEmbed(title="Uptime")
            .add_field(
                name="Duration",
                value=format_timedelta(datetime.now(UTC) - self._start_time),
            )
            .add_field(
                name="Start time", value=f"<t:{self._start_time.timestamp():.0f}>"
            )
        )

    @discord.app_commands.command(
        name="shutdown", description="Shuts down the bot globally."
    )
    async def _shutdown(self, interaction: discord.Interaction):
        if not self.bot.is_admin(interaction.user):
            raise commands.NotOwner()

        logger.critical(
            "Shutdown requested by user %s from guild %s",
            interaction.user,
            interaction.guild,
        )

        await interaction.response.send_message(
            embed=InfoEmbed(description="Shutting down...")
        )
        await self.bot.close()
