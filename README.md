# RosterBot Coordinator

Discord bot for FFXIV raid role signups, standby tracking, mount status, raid role pings, and reminders.

## Local Run

```powershell
$env:DISCORD_TOKEN="YOUR_TOKEN_HERE"
python bot.py
```

Environment variable names are documented in `.env.example` as a reference. Set them in PowerShell or Railway;
the file is not loaded automatically. The bot requires `DISCORD_TOKEN`, while the other settings have local defaults.

## Railway Deploy

1. Push this folder to a GitHub repository.
2. Create a new Railway project from that GitHub repository.
3. Add an environment variable named `DISCORD_TOKEN` with your Discord bot token.
4. Railway will install `requirements.txt` and run `python bot.py`.

The bot uses SQLite. To keep rosters across Railway deployments and restarts:

1. Add a Railway volume mounted at `/data`.
2. Add `ROSTERBOT_DB_PATH=/data/raids.db` to the worker service variables.

Without a volume, Railway's local service disk is temporary and saved rosters may be lost during a redeploy.

## Tests

```powershell
python -m unittest discover -s tests -v
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
