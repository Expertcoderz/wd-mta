"""
Abstractions for interfacing with WuzAPI and processing WhatsApp data.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any, AsyncIterable, Self

from aiohttp import ClientResponse, ClientSession, web
from typeguard import check_type, typechecked

logger = logging.getLogger(__name__)


class MessageContent:
    __slots__ = ("quote",)

    _registry: dict[str, type[Self]] = {}

    def __init__(self, data: Any, /, *, key: str) -> None:
        del key

        self.quote: MessageContent | None = None

        if not isinstance(data, dict):
            return

        if (quote_dict := data.get("contextInfo", {}).get("quotedMessage")) is not None:
            self.quote = MessageContent.from_raw(quote_dict)

    def __init_subclass__(cls, keys: tuple[str, ...]) -> None:
        for key in keys:
            cls._registry[key] = cls

    @classmethod
    def from_raw(
        cls,
        data: dict[str, Any],
        /,
    ) -> MessageContent:
        for key in data:
            if subclass := cls._registry.get(key):
                return subclass(data[key], key=key)

        return UnknownMessageContent(None, key="")


class UnknownMessageContent(MessageContent, keys=()):
    __slots__ = ()


class ReactionMessageContent(MessageContent, keys=("reactionMessage",)):
    __slots__ = ("target_id", "text")

    def __init__(self, data: Any, /, *, key: str) -> None:
        super().__init__(data, key=key)

        self.target_id = check_type(data["key"]["ID"], str)
        self.text: str | None = check_type(data.get("text"), str | None)


class TextMessageContent(MessageContent, keys=("conversation", "extendedTextMessage")):
    __slots__ = ("text",)

    def __init__(self, data: Any, /, *, key: str) -> None:
        super().__init__(data, key=key)

        self.text: str

        match key:
            case "conversation":
                self.text = check_type(data, str)
            case "extendedTextMessage":
                self.text = check_type(data["text"], str)


class MediaMessageContent(
    MessageContent, keys=("stickerMessage", "imageMessage", "videoMessage")
):
    __slots__ = (
        "caption",
        "url",
        "mimetype",
        "media_key",
        "length",
        "sha256",
        "enc_sha256",
    )

    def __init__(self, data: Any, /, *, key: str) -> None:
        super().__init__(data, key=key)

        self.caption: str | None = check_type(data.get("caption"), str | None)

        self.url = check_type(data["URL"], str)
        self.mimetype = check_type(data["mimetype"], str)
        self.media_key = check_type(data["mediaKey"], str)
        self.length = check_type(data["fileLength"], int)
        self.sha256 = check_type(data["fileSHA256"], str)
        self.enc_sha256 = check_type(data["fileEncSHA256"], str)

    async def fetch(self, client: WhatsAppClient, /) -> bytes:
        download_type = self.mimetype.split("/", 1)[0]
        if download_type == "application":
            download_type = "document"

        logger.info("Fetching %s from %s", download_type, self.url)

        # TODO: this doesn't work with stickers for some reason.

        async with client.session.post(
            f"chat/download{download_type}",
            headers={"Token": client.token},
            json={
                "Url": self.url,
                "Mimetype": self.mimetype,
                "MediaKey": self.media_key,
                "FileLength": self.length,
                "FileSHA256": self.sha256,
                "FileEncSHA256": self.enc_sha256,
            },
        ) as response:
            if response.status != HTTPStatus.OK:
                raise await client.RequestError.from_response(response)

            return base64.b64decode(
                check_type((await response.json())["data"]["Data"], str).split(",", 1)[
                    1
                ]
            )


class PollMessageContent(MessageContent, keys=("pollCreationMessage",)):
    __slots__ = ("name", "options", "multiple_allowed")

    def __init__(self, data: Any, /, *, key: str) -> None:
        super().__init__(data, key=key)

        self.name = check_type(data["name"], str)
        self.options = (
            [check_type(option["optionName"], str) for option in options]
            if (options := data.get("options")) is not None
            else None
        )
        self.multiple_allowed = (
            check_type(count, int) == 0
            if (count := data.get("selectableOptionsCount")) is not None
            else None
        )


@dataclass(kw_only=True, slots=True)
class Location:
    """Represents a location data point."""

    name: str
    latitude: int
    longtitude: int


class EventMessageContent(MessageContent, keys=()):
    name: str
    description: str | None
    is_canceled: bool
    location: Location | None
    start_time: datetime
    end_time: datetime | None
    join_url: str | None


class LocationMessageContent(MessageContent, keys=()):
    location: Location


@dataclass(kw_only=True, slots=True)
class Message:
    """Represents a WhatsApp chat message."""

    id: str
    chat_id: str
    sender_id: str
    push_name: str
    is_from_me: bool
    timestamp: datetime
    is_view_once: bool
    is_ephemeral: bool
    is_edit: bool
    content: MessageContent


@dataclass(kw_only=True, slots=True)
class Group:
    """Represents a WhatsApp group chat."""

    jid: str
    name: str
    name_set_at: datetime
    topic: str | None
    topic_set_at: datetime | None
    is_announce: bool
    is_ephemeral: bool
    is_locked: bool


@dataclass(kw_only=True, slots=True)
class User:
    """Represents a WhatsApp user."""

    status: str
    verified_name: str | None
    verified_name_issuer: str | None


@typechecked
class WhatsAppClient:
    _MESSAGE_CACHE_SIZE = 10_000
    _WEBHOOK_DATA_PATTERN = re.compile("^jsonData=(.+)&token=.*?$")

    class WhatsAppException(Exception): ...

    class RequestError(WhatsAppException):
        @classmethod
        async def from_response(cls, response: ClientResponse, /) -> Self:
            logger.error(
                "Request error at %s (HTTP %s): %s",
                response.url,
                response.status,
                await response.text(),
            )

            return cls()

    def __init__(
        self,
        /,
        *,
        session: ClientSession,
        token: str,
        webhook_host: str,
        webhook_port: int,
        dump_file_path: Path | None,
    ) -> None:
        self.session = session

        self.token = token

        self.webhook_host = webhook_host
        self.webhook_port = webhook_port

        self._webhook_server: web.AppRunner

        self._dump_io = None if dump_file_path is None else dump_file_path.open("a")

        # mapping of group JIDs to Groups
        self._group_cache: dict[str, Group] = {}

    async def on_message(self, message: Message, /) -> None:
        # dummy implementation
        del message

    async def _handle_webhook(self, request: web.Request):
        logger.info("Processing incoming webhook request from %s", request.remote)

        data_match = re.match(
            self._WEBHOOK_DATA_PATTERN,
            urllib.parse.unquote(urllib.parse.unquote_plus(await request.text())),
        )
        if not data_match:
            logger.error("Failed to match webhook data")
            return web.Response()

        data = json.loads(data_group := data_match.group(1))

        if self._dump_io is not None:
            self._dump_io.write(data_group)
            self._dump_io.write("\n")
            self._dump_io.flush()

        match data["type"]:
            case "Message":
                event = data["event"]

                message_content = MessageContent.from_raw(event["Message"])

                logger.info("Processed message content: %s", message_content)

                await self.on_message(
                    Message(
                        id=(info := event["Info"])["ID"],
                        chat_id=info["Chat"],
                        sender_id=info["Sender"],
                        is_from_me=info["IsFromMe"],
                        push_name=info["PushName"],
                        timestamp=datetime.fromisoformat(info["Timestamp"]),
                        is_ephemeral=event["IsEphemeral"],
                        is_view_once=event["IsViewOnce"]
                        or event["IsViewOnceV2"]
                        or event["IsViewOnceV2Extension"],
                        is_edit=event["IsEdit"],
                        content=message_content,
                    )
                )
            case other:
                logger.info("Ignoring unknown event: %s", other)

        return web.Response()

    async def setup(self, /):
        """Sets up the WhatsApp client, registering the webhook with WuzAPI."""
        logger.info(
            "Starting webhook server on %s:%d", self.webhook_host, self.webhook_port
        )

        app = web.Application()
        app.add_routes([web.post("/", self._handle_webhook)])
        self._webhook_server = web.AppRunner(app)

        await self._webhook_server.setup()
        await web.TCPSite(
            self._webhook_server,
            self.webhook_host,
            self.webhook_port,
            shutdown_timeout=36000000.0,
        ).start()

        logger.info("Setting up WuzAPI webhook")

        async with self.session.post(
            "webhook",
            headers={"Token": self.token},
            json={"webhookURL": f"http://{self.webhook_host}:{self.webhook_port}/"},
        ) as response:
            if response.status == HTTPStatus.OK:
                if logger.isEnabledFor(logging.INFO):
                    logger.info("Webhook setup successful: %s", await response.json())
            else:
                if logger.isEnabledFor(logging.ERROR):
                    logger.error(
                        "Webhook setup failed with HTTP %d: %s",
                        response.status,
                        await response.json(),
                    )
                raise await self.RequestError.from_response(response)

    async def connect(self, /) -> None:
        logger.info("Connecting to WuzAPI")

        async with self.session.post(
            "session/connect",
            headers={"Token": self.token},
            json={
                "Subscribe": ["Message"],
                "Immediate": "false",
            },
        ) as response:
            if response.status != HTTPStatus.OK:
                raise await self.RequestError.from_response(response)

    async def disconnect(self, /) -> None:
        logger.info("Disconnecting from WhatsApp")

        async with self.session.post(
            "session/disconnect",
            headers={"Token": self.token},
        ) as response:
            if response.status != HTTPStatus.OK:
                raise await self.RequestError.from_response(response)

    async def cleanup(self, /) -> None:
        """Cleans up state associated with the WhatsApp client."""
        logger.info("Cleaning up WhatsApp client")

        await self._webhook_server.cleanup()

        if self._dump_io is not None:
            self._dump_io.close()

    async def get_pairing_code(self, phone: str, /) -> str:
        """Requests for a pairing code for the given phone number."""
        async with self.session.get(
            "session/pairphone",
            headers={"Token": self.token},
            json={"Phone": phone},
        ) as response:
            if response.status != HTTPStatus.OK.value:
                raise await self.RequestError.from_response(response)

            return (await response.json())["data"]["LinkingCode"]

    async def _refresh_group_cache(self, /) -> None:
        async with self.session.get(
            "group/list", headers={"Token": self.token}
        ) as response:
            if response.status != HTTPStatus.OK.value:
                raise await self.RequestError.from_response(response)

            logger.info("Refreshing group cache")

            self._group_cache.clear()

            for group_entry in (await response.json())["data"]["Groups"]:
                self._group_cache[group_entry["JID"]] = Group(
                    jid=group_entry["JID"],
                    name=group_entry["Name"],
                    name_set_at=datetime.fromisoformat(group_entry["NameSetAt"]),
                    topic=group_entry["Topic"] or None,
                    topic_set_at=(
                        datetime.fromisoformat(group_entry["TopicSetAt"])
                        if group_entry["Topic"]
                        else None
                    ),
                    is_announce=group_entry["IsAnnounce"],
                    is_ephemeral=group_entry["IsEphemeral"],
                    is_locked=group_entry["IsLocked"],
                )

    async def get_groups(self, /) -> AsyncIterable[Group]:
        """Retrieves an asynchronous iterable of Groups."""
        if not self._group_cache:
            await self._refresh_group_cache()

        for group in self._group_cache.values():
            yield group

    async def get_group_by_name(self, name: str, /) -> Group | None:
        """
        Retrieves a Group by its case-insensitive name.
        Returns None if no matching Group is found.
        """
        name = name.casefold()

        async for group in self.get_groups():
            if group.name.casefold() == name:
                return group

        return None

    async def get_group_name_from_jid(self, jid: str, /) -> str | None:
        """
        Retrieves a group name from its JID.
        Returns the name, or None if no matching group is found.
        """
        if group := self._group_cache.get(jid):
            return group.name

        await self._refresh_group_cache()

        if group := self._group_cache.get(jid):
            return group.name

        return None

    async def get_users(self, jids: list[str], /) -> AsyncIterable[User]:
        """
        Gets an asynchronous iterable of users, including known contacts, by their JIDs.
        """
        async with self.session.post(
            "user/info",
            headers={"Token": self.token},
            json={"Phone": jids},
        ) as response:
            if response.status != HTTPStatus.OK.value:
                raise await self.RequestError.from_response(response)

            for user_entry in (await response.json())["data"]["Users"].values():
                yield User(
                    status=user_entry["Status"],
                    verified_name=(
                        user_entry["VerifiedName"]["verifiedName"]
                        if user_entry["VerifiedName"]
                        else None
                    ),
                    verified_name_issuer=(
                        user_entry["VerifiedName"]["issuer"]
                        if user_entry["VerifiedName"]
                        else None
                    ),
                )

    async def get_user_avatar(self, jid: str, /, *, preview: bool = False) -> str:
        """
        Retrieves a user's WhatsApp avatar.
        """
        async with self.session.post(
            "user/avatar",
            headers={"Token": self.token},
            json={"Phone": jid, "Preview": preview},
        ) as response:
            if response.status != HTTPStatus.OK.value:
                raise await self.RequestError.from_response(response)

            return (await response.json())["data"]["url"]
