import discord

from .discord import client as discord_client


@discord_client.tree.command(
    name="pair", description="Pairs with your WhatsApp account."
)
@discord.app_commands.describe(number="Your phone number.")
async def pair(interaction: discord.Interaction, number: str):
    if not discord_client.is_owner(interaction.user):
        await interaction.response.send_message(
            "You must be an owner to use this command!", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)
    await interaction.followup.send(content="fixme")
