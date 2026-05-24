# RosterBot Coordinator

Discord bot for FFXIV raid role signups, standby tracking, mount status, raid role pings, and reminders.

## Local Run

```powershell
$env:DISCORD_TOKEN="YOUR_TOKEN_HERE"
python bot.py
```

## Railway Deploy

1. Push this folder to a GitHub repository.
2. Create a new Railway project from that GitHub repository.
3. Add an environment variable named `DISCORD_TOKEN` with your Discord bot token.
4. Railway will install `requirements.txt` and run `python bot.py`.

The bot uses SQLite (`raids.db`). Railway local disk may not be permanent across all deployment scenarios, so move to Postgres later if you need long-term roster history.

## Discord Permissions

The bot invite should include:

- View Channels
- Send Messages
- Embed Links
- Read Message History
- Use Slash Commands
- Manage Roles
- Mention Everyone

For raid role assignment, the bot's server role must be above the raid role, such as `FFXIVActiveRoster`.
