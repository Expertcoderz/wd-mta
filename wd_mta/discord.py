"""
General components for interfacing with discord.py.
"""

import functools
import logging
from collections.abc import Callable
from typing import Coroutine, Self

from discord import Color, Embed, Intents, Interaction, Object, app_commands, ui
from discord.abc import User
from discord.ext import commands
from typeguard import typechecked

logger = logging.getLogger(__name__)

ActionEmbed = functools.partial(Embed, color=Color.from_rgb(255, 255, 100))
InfoEmbed = functools.partial(Embed, color=Color.from_rgb(150, 130, 255))
ErrorEmbed = functools.partial(Embed, title="Error", color=Color.from_rgb(255, 30, 50))


@typechecked
class WDMTABot(commands.Bot):
    def __init__(
        self,
        /,
        *,
        setup: Callable[[Self], Coroutine],
        cleanup: Callable[[Self], Coroutine],
        admin_ids: set[int] | None,
        sync_guild_id: int | None = None,
    ) -> None:
        intents = Intents.default()
        intents.message_content = True

        super().__init__(intents=intents, command_prefix=())

        self.tree.on_error = self._on_command_error

        # May be overriden by the driver.
        self.setup = setup
        self.cleanup = cleanup
        self.admin_ids = admin_ids
        self.sync_guild_id = sync_guild_id

    async def setup_hook(self) -> None:
        await self.setup(self)

        if self.sync_guild_id is not None:
            # Sync the commands to the specific guild.
            # This is mainly intended for development purposes.

            if logger.isEnabledFor(logging.INFO):
                logger.info(
                    "Syncing %d commands: %s",
                    len(cmds := self.tree.get_commands()),
                    ", ".join(cmd.name for cmd in cmds),
                )

            guild = Object(id=self.sync_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

        if self.admin_ids is not None:
            logger.debug("Fetching bot owner ID")
            self.admin_ids.add((await self.application_info()).owner.id)

    async def close(self) -> None:
        # This MUST come before `await super().close()`, because certain
        # state may have to be cleaned up before the event loop ends.
        await self.cleanup(self)

        await super().close()

    async def on_ready(self):
        assert self.user
        logger.info("Logged in as %s (User ID: %d)", self.user, self.user.id)

    def is_admin(self, user: User, /) -> bool:
        if self.admin_ids is None:
            return True

        return user.id in self.admin_ids

    async def _on_command_error(
        self,
        interaction: Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error.__cause__, commands.NotOwner):
            await interaction.response.send_message(
                embed=ErrorEmbed(
                    description="(╯°□°)╯︵ ┻━┻ You do not have permission to run this command!"
                )
            )
            return

        logger.exception("Command execution error", exc_info=error)

        await (
            interaction.followup.send
            if interaction.response.is_done()
            else interaction.response.send_message
        )(
            embed=ErrorEmbed(
                description=r"(´･_･\`) An error occurred while running the command."
                "\nThis is probably due to a bug."
            ).set_footer(text="Developers: check the logs for details.")
        )


@typechecked
class Paginator(ui.View):
    PAGE_SIZE = 10

    class ListHeading(str): ...

    @classmethod
    def format_list(cls, entries: list[str], /) -> str:
        return "\n".join(
            (
                f"**{entry}**"
                if isinstance(entry, cls.ListHeading)
                else f"\N{BULLET} {entry}"
            )
            for entry in entries
        )

    def __init__(
        self,
        interaction: Interaction,
        entries: list[str],
        *,
        format_list: Callable[[list[str]], str] | None = None,
        template: Embed | None = None,
    ):
        super().__init__()

        self.interaction = interaction
        self.entries = entries
        self.format_list = format_list or self.format_list
        self.template = template or InfoEmbed()

        self.page = 0

    @property
    def max_page(self, /):
        return -(len(self.entries) // -self.PAGE_SIZE) - 1

    async def interaction_check(self, interaction: Interaction, /) -> bool:
        if interaction.user == self.interaction.user:
            return True

        await interaction.response.send_message(
            embed=ErrorEmbed(description="(►__◄) This button is not for you!"),
            ephemeral=True,
        )

        return False

    def generate_embed(self, /) -> Embed:
        logger.info("Generating embed for page %d", self.page)

        start_idx = self.page * self.PAGE_SIZE

        embed = self.template.copy()
        embed.description = self.format_list(
            self.entries[start_idx : start_idx + self.PAGE_SIZE]
        )
        embed.set_footer(text=f"page {self.page + 1}/{self.max_page + 1}")

        return embed

    async def start(self, /) -> None:
        await (
            self.interaction.followup.send
            if self.interaction.response.is_done()
            else self.interaction.response.send_message
        )(embed=self.generate_embed(), view=self)

    async def show_page(self, page: int, /) -> None:
        self.page = min(max(page, 0), self.max_page)

        await self.interaction.edit_original_response(
            embed=self.generate_embed(), view=self
        )

    @ui.button(emoji="\N{BLACK LEFT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}")
    async def first_page(self, interaction: Interaction, button: ui.Button, /):
        del interaction, button
        await self.show_page(0)

    @ui.button(emoji="\N{BLACK LEFT-POINTING TRIANGLE}")
    async def prev_page(self, interaction: Interaction, button: ui.Button, /):
        del interaction, button
        await self.show_page(self.page - 1)

    @ui.button(emoji="\N{BLACK RIGHT-POINTING TRIANGLE}")
    async def next_page(self, interaction: Interaction, button: ui.Button, /):
        del interaction, button
        await self.show_page(self.page + 1)

    @ui.button(emoji="\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}")
    async def last_page(self, interaction: Interaction, button: ui.Button, /):
        del interaction, button
        await self.show_page(self.max_page)
