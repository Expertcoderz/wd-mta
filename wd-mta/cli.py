import logging
import os
import sys
from optparse import OptionParser
from pathlib import Path

from .core import client


parser = OptionParser(
    prog=(
        f"{os.path.basename(sys.executable)} -m {__package__}"
        if (prog_path := Path(sys.argv[0])).stem == "__main__"
        else prog_path.name
    ),
    usage="%prog [OPTION]...",
    epilog="The bot token may be specified via -t/--token"
    " or the WD_MTA_TOKEN environment variable; the former takes precedence."
    "\nIf no token is supplied, it will be prompted for automatically.",
)

parser.add_option(
    "-t",
    "--token",
    metavar="TOKEN",
    help="the token for the bot",
)
parser.add_option(
    "-s",
    "--sync-commands",
    type=int,
    metavar="GUILD_ID",
    help="sync commands to the guild GUILD_ID and exit immediately afterwards",
)
parser.add_option(
    "-o",
    "--owner",
    type=int,
    action="append",
    metavar="USER_ID",
    help="allow the user USER_ID to run owner commands, in addition to the actual owner"
    " (can be given multiple times to specify multiple owners)",
)
parser.add_option(
    "--allow-all", action="store_true", help="allow all users to run owner commands"
)

parser.add_option(
    "--reconnect",
    dest="reconnect",
    action="store_true",
    default=True,
    help="enable automatic reconnection (default)",
)
parser.add_option(
    "--no-reconnect",
    dest="reconnect",
    action="store_false",
    help="disable automatic reconnection",
)

parser.add_option(
    "-L",
    "--log-level",
    metavar="LEVEL",
    help="set the log level; one of: critical, error, warning, info, debug (default: info)",
)


def main() -> None:
    opts, _ = parser.parse_args()

    log_level = logging.INFO

    if opts.log_level:
        match opts.log_level.casefold():
            case "critical":
                log_level = logging.CRITICAL
            case "error":
                log_level = logging.ERROR
            case "warning":
                log_level = logging.WARNING
            case "info":
                log_level = logging.INFO
            case "debug":
                log_level = logging.DEBUG
            case _:
                parser.error(f"Unknown log level: '{opts.log_level}'")

    if opts.owner:
        if opts.allow_all:
            parser.error("--allow-all cannot be specified with -o/--owner")

        client.owner_ids = set(opts.owner)

    if opts.allow_all:
        client.owner_ids = None

    if not opts.token:
        opts.token = os.environ.get("WD_MTA_TOKEN")

        try:
            while not opts.token:
                opts.token = input("Enter the bot token: ")
        except KeyboardInterrupt:
            print()
            sys.exit(130)

    client.sync_guild_id = opts.sync_commands

    client.run(
        opts.token, reconnect=opts.reconnect, log_level=log_level, root_logger=True
    )
