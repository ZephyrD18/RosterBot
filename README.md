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

Environment variable names are documented in `.env.example` as a reference. Set them in PowerShell or Railway;
the file is not loaded automatically. The bot requires `DISCORD_TOKEN`, while the other settings have local defaults.

Raid dates and entered times use Pacific Time (PT); posted Discord timestamps display in each viewer's timezone.
Use `today`, a month/day such as `10/24`, or an explicit year such as `2027-01-01`.
Omitting the year means the current Pacific year. Past dates and times return an error instead of moving to another year.

Standby selections stay private until **Confirm Standby** is pressed. Clear the selections and confirm to leave standby.
The **Mount Status** button reopens the mount question, including on locked rosters.
Existing signup posts receive updated buttons when the roster is next refreshed.
After deletion, failed role cleanup stays saved and retries each minute while the bot can access the server.
Pending cleanup is excluded from active rosters; retry immediately using either deletion command with its signup message ID.

## Railway Deploy

1. Push this folder to a GitHub repository.
2. Create a new Railway project from that GitHub repository.
3. Add an environment variable named `DISCORD_TOKEN` with your Discord bot token.
4. Railway will install `requirements.txt` and run `python bot.py`.

The bot uses SQLite. To keep rosters across Railway deployments and restarts:

1. Add a Railway volume mounted at `/data`.
2. Add `ROSTERBOT_DB_PATH=/data/raids.db` to the worker service variables.

Without a volume, Railway's local service disk is temporary and saved rosters may be lost during a redeploy.
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
