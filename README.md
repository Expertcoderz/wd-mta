# wd-mta

WD-MTA (WhatsApp-to-Discord Message Transfer Automaton) is a Discord bot that
performs bidirectional forwarding of messages between Discord and WhatsApp.

This project is currently unfinished:
- Only WhatsApp-to-Discord forwarding has been implemented.
- Not all message types are supported. Text messages and reactions work, though.
- Beware bugs!
- No packaging, yet.

## Architecture

To interface with WhatsApp for operations such as retrieving chats and messages,
WD-MTA communicates with a separate server providing the [WuzAPI](https://github.com/asternic/wuzapi)
RESTful API over a HTTP connection. WuzAPI in turn uses the [whatsmeow](https://github.com/tulir/whatsmeow)
Go library to implement WhatsApp functionality. This design approach, rather
than communicating directly with WhatsApp, was chosen for WD-MTA due to an
apparent lack of suitable and up-to-date Python libraries for WhatsApp
operations.

Discord bot functionality is provided via the [discord.py](https://github.com/Rapptz/discord.py)
library.

The overall architecture, in terms of data flow, can be visualized as follows:

```txt
+------------------+       +--------+       +--------+       +-----------------+
| WhatsApp servers | <---> | WuzAPI | <---> | WD-MTA | <---> | Discord servers |
+------------------+       +--------+       +--------+       +-----------------+
```
