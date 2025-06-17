from __future__ import annotations

import functools
import json
import logging
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal, TypedDict

import discord
from discord.ext import commands
from typeguard import check_type, typechecked

from .. import whatsapp
from ..discord import ActionEmbed, ErrorEmbed, InfoEmbed, Paginator, WDMTABot

logger = logging.getLogger(__name__)


class SavedBindingConfiguration(TypedDict, total=False):
    discord_to_whatsapp: bool
    whatsapp_to_discord: bool


class SavedConfiguration(TypedDict, total=False):
    bindings_paused: bool
    # Channel ID is stored as str instead of int due to JSON constraints.
    bindings: dict[str, dict[str, SavedBindingConfiguration]]


@dataclass(kw_only=True, slots=True)
class ActiveBindingConfiguration:
    discord_to_whatsapp: bool = False
    whatsapp_to_discord: bool = False


@dataclass(kw_only=True, slots=True)
class ActiveConfiguration:
    bindings_paused: bool = False
    bindings: dict[str, dict[str, ActiveBindingConfiguration]] = field(
        default_factory=dict
    )


class _MessageForwardingParams(TypedDict):
    channel: discord.TextChannel
    embeds: list[discord.Embed]
    file: discord.File | None
    message_id: str
    reference_id: str | None


@typechecked
class WhatsAppCog(commands.Cog):
    def __init__(
        self,
        bot: WDMTABot,
        whatsapp_client: whatsapp.WhatsAppClient,
        *,
        config_path: Path,
        media_max_size: int,
        message_cache_size: int,
    ) -> None:
        self.bot = bot
        self.whatsapp_client = whatsapp_client
        self.config_path = config_path
        self.media_maxsize = media_max_size
        self.message_cache_size = message_cache_size

        self._config: ActiveConfiguration
        self._load_config()

        whatsapp_client.on_message = self._process_whatsapp_message

        self._whatsapp_to_discord_messages: dict[
            str, OrderedDict[int, discord.Message]
        ] = {}

    def _load_config(self, /) -> None:
        if not self.config_path.exists():
            logger.info("Creating empty configuration")
            self._config = ActiveConfiguration()
            return

        logger.info("Loading configuration from disk")
        with self.config_path.open("r") as f:
            config = check_type(json.load(f), SavedConfiguration)
            bindings: dict[str, dict[str, SavedBindingConfiguration]] = config.get(
                "bindings", {}
            )

            self._config = ActiveConfiguration(
                **(
                    {"bindings_paused": bindings_paused}
                    if (bindings_paused := config.get("bindings_paused"))
                    else {}
                ),
                bindings={
                    chat_id: {
                        channel_id: ActiveBindingConfiguration(**config)
                        for channel_id, config in chat_bindings.items()
                    }
                    for chat_id, chat_bindings in bindings.items()
                },
            )

    def _save_config(self, /) -> None:
        logger.info("Saving configuration to disk")
        with self.config_path.open("w") as f:
            json.dump(check_type(asdict(self._config), SavedConfiguration), f)

    def _format_quote(self, quote: whatsapp.MessageContent, /) -> str:
        quote_str: str

        if isinstance(quote, whatsapp.TextMessageContent):
            quote_str = quote.text
        elif isinstance(quote, whatsapp.PollMessageContent):
            quote_str = quote.name
        else:
            quote_str = "`< unsupported message content type >`"

        return f"> {discord.utils.escape_markdown(quote_str)}"

    async def _send_forwarded_message_to_channel(
        self,
        params: _MessageForwardingParams,
    ) -> None:
        channel = params["channel"]

        send_kwargs: dict[str, Any] = {}
        if params["file"] is not None:
            send_kwargs["file"] = params["file"]

        if params["reference_id"] is not None:
            reference = self._whatsapp_to_discord_messages.get(
                params["reference_id"], {}
            ).get(channel.id)

            if reference is None:
                params["embeds"].append(
                    ErrorEmbed(
                        description="The referenced message could not be retrieved."
                    ),
                )
            else:
                send_kwargs["reference"] = reference

        store = self._whatsapp_to_discord_messages.get(params["message_id"])
        if store is None:
            store = self._whatsapp_to_discord_messages[params["message_id"]] = (
                OrderedDict()
            )

        store[channel.id] = await channel.send(embeds=params["embeds"], **send_kwargs)

        if len(store) > self.message_cache_size:
            store.popitem(last=False)

    async def _process_whatsapp_message(self, message: whatsapp.Message, /) -> None:
        if self._config.bindings_paused:
            return

        if not (chat_bindings := self._config.bindings.get(message.chat_id)):
            return

        initial_embeds = [discord.Embed(timestamp=message.timestamp)]
        initial_embeds[0].set_footer(text="forwarded from WhatsApp")

        avatar_url: str | None = None
        try:
            avatar_url = await self.whatsapp_client.get_user_avatar(
                message.sender_id,
                preview=True,
            )
        except whatsapp.WhatsAppClient.RequestError:
            pass

        initial_embeds[0].set_author(
            name=message.push_name,
            icon_url=avatar_url,
        )

        if message.content.quote is not None:
            initial_embeds.insert(
                0, discord.Embed(description=self._format_quote(message.content.quote))
            )

        for channel_id, config in chat_bindings.items():
            if not config.whatsapp_to_discord:
                continue

            if (channel := self.bot.get_channel(int(channel_id))) is None:
                logger.warning("Bound channel is nonexistent")
                del chat_bindings[channel_id]
                continue

            if not isinstance(channel, discord.TextChannel):
                logger.warning("Bound channel is not a TextChannel: %s", channel)
                del chat_bindings[channel_id]
                continue

            logger.info(
                "Forwarding WhatsApp message from %s to channel %s",
                message.push_name,
                channel,
            )

            await self._handle_message_content(
                message.content,
                _MessageForwardingParams(
                    channel=channel,
                    embeds=initial_embeds,
                    file=None,
                    message_id=message.id,
                    reference_id=None,
                ),
            )

    @functools.singledispatchmethod
    async def _handle_message_content(
        self, content: whatsapp.MessageContent, params: _MessageForwardingParams
    ):
        del content
        params["embeds"][0].description = "`< unsupported message content type >`"
        await self._send_forwarded_message_to_channel(params)

    @_handle_message_content.register
    async def _(
        self,
        content: whatsapp.UnknownMessageContent,
        params: _MessageForwardingParams,
    ):
        del content, params

    @_handle_message_content.register
    async def _(
        self,
        content: whatsapp.ReactionMessageContent,
        params: _MessageForwardingParams,
    ):
        if content.text is None:
            params["embeds"][-1].title = "Reaction Removed"
        else:
            params["embeds"][-1].title = "Reaction Added"
            params["embeds"][-1].description = content.text

        params["reference_id"] = content.target_id

        await self._send_forwarded_message_to_channel(params)

    @_handle_message_content.register
    async def _(
        self,
        content: whatsapp.TextMessageContent,
        params: _MessageForwardingParams,
    ):
        params["embeds"][-1].description = discord.utils.escape_markdown(content.text)
        await self._send_forwarded_message_to_channel(params)

    @_handle_message_content.register
    async def _(
        self,
        content: whatsapp.MediaMessageContent,
        params: _MessageForwardingParams,
    ):
        params["embeds"][-1].description = content.caption

        try:
            if content.length > self.media_maxsize:
                raise ValueError()

            data = await content.fetch(self.whatsapp_client)
        except ValueError:
            params["embeds"].append(
                ErrorEmbed(
                    description=f"File exceeded size limit ({self.media_maxsize} bytes)."
                )
            )
            await self._send_forwarded_message_to_channel(params)
        except whatsapp.WhatsAppClient.RequestError:
            params["embeds"].append(ErrorEmbed(description="Failed to download media."))
            await self._send_forwarded_message_to_channel(params)
        else:
            with NamedTemporaryFile(
                "w+b", suffix="." + content.mimetype.split("/", 1)[1]
            ) as f:
                logger.info("Writing %d B of data to temporary file", len(data))
                f.write(data)
                f.flush()

                params["file"] = discord.File(f.name)
                await self._send_forwarded_message_to_channel(params)

    _session_group = discord.app_commands.Group(
        name="session", description="WhatsApp session management."
    )

    @_session_group.command(
        name="pair", description="Pairs with your WhatsApp account."
    )
    @discord.app_commands.describe(
        phone="Your phone number, including the country code but excluding non-numeric characters.",
    )
    async def _session_pair(self, interaction: discord.Interaction, phone: int):
        if not self.bot.is_admin(interaction.user):
            raise commands.NotOwner()

        await interaction.response.defer(ephemeral=True)

        code = self.whatsapp_client.get_pairing_code(str(phone))
        await interaction.followup.send(
            embed=ActionEmbed(title="Phone Pairing").add_field(
                name="Pairing code", value=f"`{code}`"
            )
        )

    @_session_group.command(
        name="logout", description="Signs out of the WhatsApp session."
    )
    async def _session_logout(self, interaction: discord.Interaction):
        if not self.bot.is_admin(interaction.user):
            raise commands.NotOwner()

        await interaction.response.defer()
        await self.whatsapp_client.disconnect()
        await interaction.followup.send(
            embed=InfoEmbed(
                title="Logout",
                description="You have successfully signed out from the WhatsApp session."
                "\n\nUse the `/session pair` command to sign in again.",
            )
        )

    _user_group = discord.app_commands.Group(
        name="user", description="WhatsApp user operations."
    )

    @_user_group.command(name="info", description="Fetches user information.")
    @discord.app_commands.describe(
        phones="The phone number(s) of the user(s) to query. May be a comma-separated list."
    )
    async def _user_info(self, interaction: discord.Interaction, phones: str):
        if not self.bot.is_admin(interaction.user):
            raise commands.NotOwner()

        await interaction.response.defer()

        async for user in self.whatsapp_client.get_users(
            list(map("{}@s.whatsapp.net".format, map(str.strip, phones.split(","))))
        ):
            embed = InfoEmbed(title="User Information")
            embed.add_field(
                name="Status",
                value=discord.utils.escape_markdown(user.status),
            )

            if user.verified_name is not None:
                embed.add_field(
                    name="Verified name",
                    value=discord.utils.escape_markdown(user.verified_name),
                    inline=False,
                )

            await interaction.followup.send(embed=embed)

    @_user_group.command(name="avatar", description="Fetches a user's avatar.")
    @discord.app_commands.describe(
        phone="The phone number of the target user,"
        "\n including the country code but excluding non-numeric characters."
    )
    async def _user_avatar(self, interaction: discord.Interaction, phone: str):
        if not self.bot.is_admin(interaction.user):
            raise commands.NotOwner()

        await interaction.response.defer()

        url = await self.whatsapp_client.get_user_avatar(f"{phone}@s.whatsapp.net")
        await interaction.followup.send(url)

    _group_group = discord.app_commands.Group(
        name="group", description="WhatsApp group chat operations."
    )

    @_group_group.command(name="list", description="Lists WhatsApp group chats.")
    @discord.app_commands.describe(
        name_contains="Filter entries based on whether their names contain a certain string.",
        is_announce="Filter entries based on whether they are announcement groups.",
    )
    async def _group_list(
        self,
        interaction: discord.Interaction,
        name_contains: str | None,
        is_announce: bool | None,
    ):
        if not self.bot.is_admin(interaction.user):
            raise commands.NotOwner()

        await interaction.response.defer(ephemeral=True)

        if name_contains:
            name_contains = name_contains.casefold()

        filtered_groups: list[str] = []

        async for group in self.whatsapp_client.get_groups():
            if name_contains and name_contains not in group.name.casefold():
                continue

            if is_announce is not None and group.is_announce != is_announce:
                continue

            filtered_groups.append(group.name)

        await Paginator(interaction, filtered_groups).start()

    @_group_group.command(
        name="info", description="Displays WhatsApp group information."
    )
    @discord.app_commands.describe(name="The name of the group chat. Case-insensitive.")
    async def _group_info(self, interaction: discord.Interaction, name: str):
        if not self.bot.is_admin(interaction.user):
            raise commands.NotOwner()

        await interaction.response.defer()

        group = await self.whatsapp_client.get_group_by_name(name)
        if group is None:
            await interaction.followup.send(
                embed=ErrorEmbed(description="Group not found.")
            )
            return

        embed = InfoEmbed(title="Group Information")
        embed.add_field(name="ID", value=group.jid)
        embed.add_field(name="Name", value=group.name)
        embed.add_field(
            name="Name set at", value=group.name_set_at.strftime("%Y-%m-%d %T")
        )

        embed.add_field(
            name="Announcement group", value=str(group.is_announce), inline=False
        )
        if group.topic is not None:
            embed.add_field(name="Topic", value=group.topic)
            if group.topic_set_at is not None:
                embed.add_field(
                    name="Topic set at",
                    value=group.topic_set_at.strftime("%Y-%m-%d %T"),
                )

        await interaction.followup.send(embed=embed)

    _binding_group = discord.app_commands.Group(
        name="binding", description="WhatApp-Discord binding management."
    )

    @_binding_group.command(name="list", description="Lists all configured bindings.")
    @discord.app_commands.describe(
        include_global="Include bindings from all servers, not just the current server."
    )
    async def _binding_list(
        self, interaction: discord.Interaction, include_global: bool = False
    ):
        if include_global and not self.bot.is_admin(interaction.user):
            await interaction.response.send_message(
                embed=ErrorEmbed(
                    description="For security, only owners can list bindings globally."
                )
            )
            return

        if not self._config.bindings:
            await interaction.response.send_message(
                embed=InfoEmbed(description="No bindings have been configured.")
            )
            return

        await interaction.response.defer()

        result: list[str | Paginator.ListHeading] = []

        for chat_id, chat_bindings in self._config.bindings.items():
            result.append(
                Paginator.ListHeading(
                    await self.whatsapp_client.get_group_name_from_jid(chat_id)
                )
            )

            for channel_id, config in chat_bindings.items():
                result.append(
                    f"<#{channel_id}> (W {
                    "<" if config.discord_to_whatsapp else ""
                    }-{
                    ">" if config.whatsapp_to_discord else ""
                    } D)"
                )

        await Paginator(interaction, result).start()

    @_binding_group.command(
        name="set",
        description="Binds a channel to a WhatsApp group chat, or updates an existing binding.",
    )
    @discord.app_commands.describe(
        group_name="The name of the WhatsApp group chat.",
        channel="The corresponding Discord channel. If unspecified, one will be automatically created.",
        direction="The direction to which messages should be forwarded.",
    )
    async def _binding_set(
        self,
        interaction: discord.Interaction,
        group_name: str,
        channel: discord.TextChannel | None,
        direction: Literal[
            "Discord to WhatsApp", "WhatsApp to Discord", "Bidirectional"
        ],
    ):
        if not self.bot.is_admin(interaction.user):
            raise commands.NotOwner()

        await interaction.response.defer()

        group_name = group_name.casefold()

        group = await self.whatsapp_client.get_group_by_name(group_name)
        if group is None:
            await interaction.followup.send(
                embed=ErrorEmbed(description="Group not found.")
            )
            return

        if channel is None:
            assert interaction.guild
            channel = await interaction.guild.create_text_channel(group.name)

        chat_bindings = self._config.bindings.get(group.jid, {})
        if not chat_bindings:
            self._config.bindings[group.jid] = chat_bindings

        config = chat_bindings.get(str(channel.id))
        updating = True
        if config is None:
            config = chat_bindings[str(channel.id)] = ActiveBindingConfiguration(
                discord_to_whatsapp=direction[0] in set("DB"),
                whatsapp_to_discord=direction[0] in set("WB"),
            )
            updating = False

        await interaction.followup.send(
            embed=InfoEmbed(
                title=(
                    "Existing Binding Updated" if updating else "New Binding Created."
                )
            )
            .add_field(
                name="Chat name", value=discord.utils.escape_markdown(group.name)
            )
            .add_field(name="Channel", value=f"<#{channel.id}>")
            .add_field(name="Forwarding direction", value=direction)
        )

        self._save_config()

    @_binding_group.command(
        name="remove",
        description="Unbinds a channel from a WhatsApp group chat.",
    )
    @discord.app_commands.describe(
        channel="The corresponding Discord channel.",
        preserve_channel="Whether to keep (i.e. avoid deleting) the channel. Defaults to `False`.",
    )
    async def _binding_remove(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        preserve_channel: bool = False,
    ):
        if not self.bot.is_admin(interaction.user):
            raise commands.NotOwner()

        await interaction.response.defer()

        if not self._config.bindings:
            await interaction.followup.send(
                embed=InfoEmbed(description="No bindings are currently configured.")
            )
            return

        for chat_id, chat_bindings in self._config.bindings.items():
            if str(channel.id) in chat_bindings:
                del chat_bindings[str(channel.id)]
                if not chat_bindings:
                    del self._config.bindings[chat_id]
                break
        else:
            await interaction.followup.send(
                embed=InfoEmbed(
                    description="No binding is configured for the given channel."
                )
            )
            return

        if not preserve_channel:
            await channel.delete()

        if preserve_channel or channel != interaction.channel:
            await interaction.followup.send(
                embed=InfoEmbed(description="Binding successfully deleted.")
            )

        self._save_config()

    @_binding_group.command(
        name="clear", description="Removes all configured bindings."
    )
    @discord.app_commands.describe(
        preserve_channels="Whether to keep (i.e. avoid deleting) the channels. Defaults to `False`.",
        include_global="Include bindings from all servers, not just the current server.",
    )
    async def _binding_clear(
        self,
        interaction: discord.Interaction,
        preserve_channels: bool = False,
        include_global: bool = False,
    ):
        if not self.bot.is_admin(interaction.user):
            raise commands.NotOwner()

        if not self._config.bindings:
            await interaction.response.send_message(
                embed=InfoEmbed(description="There are no bindings to clear.")
            )

        await interaction.response.defer()

        assert interaction.guild

        cleared_count = 0
        failed_count = 0
        missing_count = 0

        for chat_id, chat_bindings in self._config.bindings.items():
            to_clear: set[str] = set()

            for channel_id in chat_bindings:
                channel = (
                    self.bot.get_channel(int(channel_id))
                    if include_global
                    else interaction.guild.get_channel(int(channel_id))
                )

                if isinstance(channel, discord.TextChannel):
                    to_clear.add(channel_id)

                    if not preserve_channels:
                        try:
                            await channel.delete()
                        except (discord.Forbidden, discord.NotFound):
                            failed_count += 1
                elif include_global:
                    to_clear.add(channel_id)
                    missing_count += 1

            cleared_count += len(to_clear)

            for channel_id in to_clear:
                del chat_bindings[channel_id]

        for chat_id in set(self._config.bindings.keys()):
            if not self._config.bindings[chat_id]:
                del self._config.bindings[chat_id]

        embeds = [
            InfoEmbed(
                description=f"Bindings have been cleared {"globally" if include_global else "for this server"}.",
            ).add_field(name="# cleared bindings", value=cleared_count),
        ]

        if failed_count > 0:
            embeds.append(
                ErrorEmbed(
                    description=f"Failed to delete channels for {failed_count} binding(s)."
                    "\nThese bindings have been cleared anyway."
                )
            )

        if include_global and missing_count > 0:
            embeds.append(
                InfoEmbed(
                    description=f"The channels for {missing_count} binding(s) were not found."
                    "\nThese bindings have been cleared anyway."
                )
            )

        await interaction.followup.send(embeds=embeds)

        self._save_config()

    @_binding_group.command(name="pause", description="Suspends all bindings globally.")
    async def _binding_pause(self, interaction: discord.Interaction):
        if not self.bot.is_admin(interaction.user):
            raise commands.NotOwner()

        if self._config.bindings_paused:
            await interaction.response.send_message(
                embed=InfoEmbed(description="Bindings are already paused.")
            )
            return

        self._config.bindings_paused = True
        await interaction.response.send_message(
            embed=InfoEmbed(
                title="Bindings Paused",
                description="All bindings have been suspended."
                "\n\nNo messages will be forwarded between WhatsApp and Discord"
                " until `/binding resume` is executed.",
            )
        )

    @_binding_group.command(name="resume", description="Resumes all bindings globally.")
    async def _binding_resume(self, interaction: discord.Interaction):
        if not self.bot.is_admin(interaction.user):
            raise commands.NotOwner()

        if not self._config.bindings_paused:
            await interaction.response.send_message(
                embed=InfoEmbed(description="Bindings have not already been paused.")
            )
            return

        self._config.bindings_paused = True
        await interaction.response.send_message(
            embed=InfoEmbed(
                title="Bindings Resumed",
                description="All bindings are now active again."
                "\n\nForwarding messages between WhatsApp and Discord is now allowed.",
            )
        )
