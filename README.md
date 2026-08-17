# MIDAS I (Python port)

Python (discord.py + firebase-admin) port of [honerableity/Midas-I](https://github.com/honerableity/Midas-I),
a Discord shop-manager bot: Roblox verification, moderation, product catalog
with forum posts, and a ticket system (order / service / customer service).

Ported module-for-module from the original Node.js source:

| Node.js               | Python                  |
|------------------------|--------------------------|
| `index.js`             | `bot.py`                 |
| `deploy-commands.js`   | `deploy_commands.py`     |
| `utils/firebase.js`    | `utils/firebase.py`      |
| `utils/logger.js`      | `utils/logger.py`        |
| `utils/moderation.js`  | `utils/moderation.py`    |
| `utils/products.js`    | `utils/products.py`      |
| `utils/tickets.js`     | `utils/tickets.py`       |
| `utils/verification.js`| `utils/verification.py`  |
| `data/words.js`        | `data/words.py`          |
| `commands/log.js`      | `commands/log.py`        |
| `commands/verify.js`   | `commands/verify.py`     |
| `commands/mod.js`      | `commands/mod.py`        |
| `commands/product.js`  | `commands/product.py`    |
| `commands/ticket.js`   | `commands/ticket.py`     |

## Setup

1. **Python 3.11+** required (uses `X | None` union syntax).
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in:
   - `DISCORD_TOKEN` — your bot token
   - `CLIENT_ID` — your Discord application ID
   - `GUILD_ID` — the server to deploy slash commands to
4. Copy `serviceAccountKey.json.example` to `serviceAccountKey.json` (project root)
   and fill it in with your real Firebase service account key
   (Firebase Console > Project Settings > Service Accounts > Generate new private key).
5. Run the bot:
   ```
   python bot.py
   ```

Slash commands auto-deploy to `GUILD_ID` every time the bot boots. To deploy
without starting the full bot, run:
```
python deploy_commands.py
```

## Commands

- `/verify start | setrole | unverify | profile` — Roblox account verification
- `/mod ban | kick | unban | mute | vcmute | unmute | unvcmute | warn | setwarn | membercount | honeypot` — moderation
- `/product create | createtype | linktype | sendpost | edit | view | delete | give | revoke | get` — shop catalog
- `/ticket send | done | settesti | createcategory | close` — ticket system
- `/log setcategory | update` — activity log channel setup

## Notes on the port

- Discord.js `Collection`/event-router patterns became discord.py `Cog` +
  `app_commands.Group` classes; command state lives in `discord.ui.View` /
  `discord.ui.Modal` subclasses instead of manual `awaitModalSubmit` /
  `awaitMessageComponent` collectors.
- The ticket panel (`/ticket send`) uses a **persistent view**
  (`timeout=None`, static `custom_id`s) registered on bot startup so its
  buttons/selects keep working after a restart — same intent as the original
  routing `ticket_*` custom IDs through a global `interactionCreate` handler.
- Firestore access uses the synchronous `firebase-admin` Python SDK (there's
  no official async Firestore client for `firebase-admin`); calls are wrapped
  in `async def` functions for interface parity with the Node version, but
  the underlying I/O is blocking. For a busy bot, consider moving Firestore
  calls to `asyncio.to_thread(...)`.
- All original business logic, validation, and error-handling comments were
  preserved as closely as Python allows.
