# RosterBot Coordinator

Discord bot for FFXIV raid role signups, standby tracking, mount status, raid role pings, and reminders.

## Local Run

Use Python 3.12 and install dependencies into a separate virtual environment:

```powershell
py -3.12 -m venv "$env:LOCALAPPDATA\RosterBot\venv"
& "$env:LOCALAPPDATA\RosterBot\venv\Scripts\python.exe" -m pip install -r requirements.txt
$env:DISCORD_TOKEN="YOUR_TOKEN_HERE"
& "$env:LOCALAPPDATA\RosterBot\venv\Scripts\python.exe" bot.py
```

Environment variable names are documented in `.env.example` as a reference. Set them in PowerShell or the hosting panel's Environment tab;
the file is not loaded automatically. The bot requires `DISCORD_TOKEN`, while the other settings have local defaults.

Raid dates and entered times use Pacific Time (PT); posted Discord timestamps display in each viewer's timezone.
Use `today`, a month/day such as `10/24`, or an explicit year such as `2027-01-01`.
Omitting the year means the current Pacific year. Past dates and times return an error instead of moving to another year.

Standby selections stay private until **Confirm Standby** is pressed. Clear the selections and confirm to leave standby.
The **Mount Status** button reopens the mount question, including on locked rosters.
Existing signup posts receive updated buttons when the roster is next refreshed.
After deletion, failed role cleanup stays saved and retries each minute while the bot can access the server.
Pending cleanup is excluded from active rosters; retry immediately using either deletion command with its signup message ID.

## bot-hosting.net

The only source files needed to run the bot are `bot.py` and `requirements.txt`.

1. Use the Python runtime; Python 3.12 is the tested version. Select the version in the panel.
2. Upload both files to the deployment root, or sync this repository's `main` branch.
3. In Startup, set **Entry File (STARTUP_FILE)** to `bot.py`. The host installs `requirements.txt` at startup.
4. In Environment, set `DISCORD_TOKEN` and mark it secret. Start or restart the deployment and check Console.

See the official [setup guide](https://bot-hosting.net/docs/guides/set-up-a-deployment)
and [Python environment support](https://bot-hosting.net/python-hosting).

The bot stores rosters in `raids.db` in the deployment root by default. Keep that file when updating;
retain `ROSTERBOT_DB_PATH` if you already use a custom database location. Stop the bot before making a database backup.
For GitHub updates, select **Merge** to preserve the existing database; **Replace all files** deletes existing server files.
See the [GitHub sync guide](https://bot-hosting.net/docs/guides/clone-a-github-repository).
Remove obsolete startup overrides and let the Python runtime launch `bot.py`.
Run one bot instance against the database; its interaction and role locks coordinate tasks within that process.

## Dependencies

Dependencies stay in the Python environment and are not bundled with the bot source.
`requirements.txt` also pins two HTTP dependencies to patched versions.

| Package | Source | License |
| --- | --- | --- |
| discord.py | [Rapptz/discord.py](https://github.com/Rapptz/discord.py) | MIT |
| aiosqlite | [omnilib/aiosqlite](https://github.com/omnilib/aiosqlite) | MIT |
| aiohttp | [aio-libs/aiohttp](https://github.com/aio-libs/aiohttp) | Apache-2.0 AND MIT |
| multidict | [aio-libs/multidict](https://github.com/aio-libs/multidict) | Apache-2.0 |

## Tests

```powershell
& "$env:LOCALAPPDATA\RosterBot\venv\Scripts\python.exe" -m unittest discover -s tests -v
```

## Discord Permissions

The bot invite should include:

- View Channels
- Send Messages
- Embed Links
- Read Message History
- Use Slash Commands
- Manage Messages
- Manage Roles
- Mention Everyone

For raid role assignment, the bot's server role must be above the raid role, such as `FFXIVActiveRoster`.
Raid roles must be ordinary member roles without moderation or server management permissions.
Changing the raid role requires both Manage Messages and Manage Roles, and the caller's role must be above it
(except for the server owner).
