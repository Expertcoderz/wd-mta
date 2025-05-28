import logging

import discord
from discord.ext import commands
from typeguard import typechecked

from ..discord import ActionEmbed, ErrorEmbed, InfoEmbed, Paginator, WDMTABot

logger = logging.getLogger(__name__)


@typechecked
class TestCog(commands.GroupCog, group_name="test"):
    def __init__(self, bot: WDMTABot, /):
        self.bot = bot

    @discord.app_commands.command(name="embeds", description="Shows some embeds.")
    async def _embeds(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embeds=[
                InfoEmbed(description="Information embed."),
                ErrorEmbed(description="Error embed."),
                ActionEmbed(description="Action embed."),
            ]
        )

    @discord.app_commands.command(name="pages", description="Shows a pagination view.")
    async def _pages(self, interaction: discord.Interaction):
        await Paginator(
            interaction,
            [
                (
                    Paginator.ListHeading(f"heading {i // 10}")
                    if i % 10 == 0
                    else f"item {i}"
                )
                for i in range(50)
            ],
        ).start()
