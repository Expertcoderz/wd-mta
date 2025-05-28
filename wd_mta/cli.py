"""
Command-line interface for launching the WD-MTA
Discord bot with various user-defined parameters.
"""

import logging
import os
import sys
from optparse import OptionParser
from pathlib import Path

from aiohttp import ClientSession

from .cogs.core import CoreCog
from .cogs.test import TestCog
from .cogs.whatsapp import WhatsAppCog
from .discord import WDMTABot
from .whatsapp import WhatsAppClient

parser = OptionParser(
    prog=(
        f"{os.path.basename(sys.executable)} -m {__package__}"
        if (prog_path := Path(sys.argv[0])).stem == "__main__"
        else prog_path.name
    ),
    usage="%prog [OPTION]...",
    description="Start WD-MTA (WhatsApp-Discord Message Transfer Automation),"
    " a Discord bot to forward chat messages between WhatsApp and Discord.",
)

parser.add_option(
    "-L",
    "--log-level",
    default="info",
    metavar="LEVEL",
    help="set the log level;"
    " one of: critical, error, warning, info, debug (default: info)",
)

parser.add_option(
    "-c",
    "--config",
    default="config.json",
    metavar="FILE",
    help="the path to the JSON configuration file (default: config.json)",
)

discord_group = parser.add_option_group("Discord bot options")
wuzapi_group = parser.add_option_group("WuzAPI options")

discord_group.add_option(
    "-t",
    "--token",
    dest="discord_token",
    metavar="TOKEN",
    help="the bot token (default: taken from WDMTA_DISCORD_TOKEN)",
)
discord_group.add_option(
    "-s",
    "--sync-to-guild",
    type=int,
    metavar="GUILD_ID",
    help="sync application commands to the guild GUILD_ID upon startup",
)
discord_group.add_option(
    "-o",
    "--owner",
    type=int,
    action="append",
    metavar="USER_ID",
    help="allow the user USER_ID to run owner commands, in addition to the actual owner"
    " (can be given multiple times to specify multiple owners)",
)
discord_group.add_option(
    "--allow-all", action="store_true", help="allow all users to run owner commands"
)

discord_group.add_option(
    "--reconnect",
    dest="reconnect",
    action="store_true",
    default=True,
    help="enable automatic reconnection (default)",
)
discord_group.add_option(
    "--no-reconnect",
    dest="reconnect",
    action="store_false",
    help="disable automatic reconnection",
)
discord_group.add_option(
    "--enable-test-commands",
    action="store_true",
    help="enable commands intended for testing purposes",
)

wuzapi_group.add_option(
    "-u",
    "--url",
    dest="wuzapi_url",
    default="http://127.0.0.1:8080",
    metavar="URL",
    help="the endpoint URL (default: http://localhost:8080)",
)
wuzapi_group.add_option(
    "-x",
    dest="wuzapi_token",
    metavar="TOKEN",
    help="the user token (default: taken from WDMTA_WUZAPI_TOKEN)",
)

wuzapi_group.add_option(
    "--webhook-host",
    dest="wuzapi_webhook_host",
    default="localhost",
    metavar="HOSTNAME",
    help="host the webhook on HOSTNAME (default: localhost)",
)
wuzapi_group.add_option(
    "--webhook-port",
    dest="wuzapi_webhook_port",
    type=int,
    default=8000,
    metavar="NUMBER",
    help="host the webhook on port NUMBER (default: 8000)",
)
wuzapi_group.add_option(
    "-m",
    "--media-maxsize",
    default=10_000_000,
    metavar="SIZE",
    help="do not download media files beyond SIZE bytes (default: 10000000)",
)
wuzapi_group.add_option(
    "-l",
    "--message-limit",
    default=1000,
    metavar="NUMBER",
    help="keep track of no more than NUMBER messages, on a per-chat basis"
    " (used for handling replies) (default: 1000)",
)
wuzapi_group.add_option(
    "-d",
    "--dump-file",
    metavar="FILE",
    help="append received WuzAPI event data to FILE (for development purposes)",
)


def parse_log_level(log_level: str) -> int:
    match log_level.casefold():
        case "critical":
            return logging.CRITICAL
        case "error":
            return logging.ERROR
        case "warning":
            return logging.WARNING
        case "info":
            return logging.INFO
        case "debug":
            return logging.DEBUG
        case _:
            parser.error(f"Unknown log level: '{log_level}'")


def get_token(*, desc: str, env_var_name: str) -> str:
    token = os.environ.get(env_var_name)

    while not token:
        token = input(f"Enter the {desc} token: ")

    return token


def main() -> int:
    opts, _ = parser.parse_args()

    log_level = parse_log_level(opts.log_level)

    owner_ids: set[int] | None = None

    if opts.owner:
        if opts.allow_all:
            parser.error("--allow-all cannot be specified with -o/--owner")

        owner_ids = set(opts.owner)
    elif not opts.allow_all:
        owner_ids = set()

    try:
        discord_token = opts.discord_token or get_token(
            desc="Discord bot", env_var_name="WDMTA_DISCORD_TOKEN"
        )
        wuzapi_token = opts.wuzapi_token = get_token(
            desc="WuzAPI", env_var_name="WDMTA_WUZAPI_TOKEN"
        )
    except KeyboardInterrupt:
        print()
        return 130

    async def setup_bot(bot: WDMTABot):
        whatsapp_client = WhatsAppClient(
            session=ClientSession(opts.wuzapi_url.rstrip("/") + "/"),
            token=wuzapi_token,
            webhook_host=opts.wuzapi_webhook_host,
            webhook_port=opts.wuzapi_webhook_port,
            dump_file_path=(None if opts.dump_file is None else Path(opts.dump_file)),
        )

        await bot.add_cog(
            WhatsAppCog(
                bot,
                whatsapp_client,
                config_path=Path(opts.config),
                media_max_size=opts.media_maxsize,
                message_cache_size=opts.message_limit,
            )
        )

        try:
            await whatsapp_client.setup()
        except WhatsAppClient.RequestError:
            await whatsapp_client.session.close()

        await bot.add_cog(CoreCog(bot))
        if opts.enable_test_commands:
            await bot.add_cog(TestCog(bot))

    async def cleanup_bot(bot: WDMTABot) -> None:
        if cog := bot.get_cog("WhatsAppCog"):
            assert isinstance(cog, WhatsAppCog)
            await cog.whatsapp_client.cleanup()
            await cog.whatsapp_client.session.close()

    bot = WDMTABot(
        setup=setup_bot, cleanup=cleanup_bot, sync_guild_id=opts.sync_to_guild
    )
    bot.owner_ids = owner_ids

    bot.run(
        discord_token,
        reconnect=opts.reconnect,
        log_level=log_level,
        root_logger=True,
    )

    return 0
