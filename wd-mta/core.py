import logging
from datetime import UTC, datetime

import discord

logger = logging.getLogger(__name__)


class WDMTAClient(discord.Client):
    def __init__(self, /) -> None:
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(intents=intents)

        self.tree = discord.app_commands.CommandTree(self)

        # If None, means that all users are permitted to run owner commands.
        self.owner_ids: set[int] | None = set()

        self.sync_guild_id: int | None = None

    async def setup_hook(self) -> None:
        if self.sync_guild_id is not None:
            # Special case: sync the commands to the specific guild
            # and close immediately afterwards.
            # This is intended for development purposes only

            guild = discord.Object(id=self.sync_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            await self.close()
            return

        if self.owner_ids is not None:
            logger.debug("Fetching bot owner ID")
            self.owner_ids.add((await self.application_info()).owner.id)

    def is_owner(self, user: discord.User | discord.Member, /) -> bool:
        return self.owner_ids is None or user.id in self.owner_ids


client = WDMTAClient()


@client.event
async def on_ready():
    assert client.user
    logger.info(f"Logged in as {client.user} (User ID: {client.user.id})")


@client.tree.command(name="ping", description="Displays the bot's ping.")
async def ping(interaction: discord.Interaction):
    delta = datetime.now(UTC) - interaction.created_at

    await interaction.response.send_message(
        f"Pong! ({delta.total_seconds() * 1000:.0f} ms)"
    )


@client.tree.command(
    name="shutdown", description="Shuts down the bot globally. (Owner-only.)"
)
async def shutdown(interaction: discord.Interaction):
    if not client.is_owner(interaction.user):
        await interaction.response.send_message(
            "Only owners can shut down the bot!", ephemeral=True
        )
        return

    logger.critical(
        f"Shutdown requested by user {interaction.user} from guild {interaction.guild}"
    )

    await interaction.response.send_message("Shutting down...")
    await client.close()


@client.tree.command(name="say", description="Makes the bot talk.")
@discord.app_commands.describe(
    text="The message content to send.", reference="The message to reply to."
)
async def say(interaction: discord.Interaction, text: str, reference: str | None):
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Not a text channel.", ephemeral=True)
        return

    # Create a temporary response to avoid a
    # red error about the bot not responding.
    await interaction.response.defer(ephemeral=True)

    send_args = {}

    if reference is not None:
        try:
            send_args["reference"] = await interaction.channel.fetch_message(
                int(reference)
            )
        except:
            await interaction.followup.send(
                content="Failed to retrieve the reference message;"
                " is it a valid ID of a message in the same channel?",
                ephemeral=True,
            )
            return

    logger.info(
        f"`/say` command ran by user {interaction.user}",
        extra={
            "guild": interaction.guild,
            "channel": interaction.channel,
            "text": text,
        },
    )

    await interaction.channel.send(text, **send_args)

    await interaction.delete_original_response()
