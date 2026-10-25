# RosterBot Coordinator

Discord bot for FFXIV raid signups, job selection, standby offers, ready checks, and roster-specific reminders.

## Signups and Jobs

Click a role, choose a job from its menu, then click **Yes** or **No** for the mount question.
For example: **Main Tank → Warrior → No**. The roster displays the job and mount answer together.
Use **Job** or **Mount Status** to update your answers. Moving slots preserves compatible jobs and mount answers;
a job picker from your old slot cannot change your new signup.

| Slots | Clickable job choices |
| --- | --- |
| Main Tank / Off Tank | Paladin, Warrior, Dark Knight, Gunbreaker |
| Healer 1 / Healer 2 | White Mage, Scholar, Astrologian, Sage |
| Melee 1 / Melee 2 | Monk, Dragoon, Ninja, Samurai, Reaper, Viper |
| Physical Ranged | Bard, Machinist, Dancer |
| Caster | Black Mage, Summoner, Red Mage, Pictomancer |

These are the 21 standard raid jobs, grouped using the official [FFXIV job guide](https://na.finalfantasyxiv.com/jobguide/battle/).
Limited jobs are excluded from this standard raid composition. No game artwork is bundled.

New raids use one blue-and-gold roster card showing occupancy, jobs, mount counts, duration, and readiness.
Old separate signup/roster posts consolidate on their next successful refresh.

## Running a Raid

- `/raidsetup` creates a card and announcement; `/raidpreset` creates a card from a preset. Choose a duration from the menu;
  new raids default to **three hours**. Existing saved raids retain their former two-hour duration.
- `/editraid` changes the name, group, meeting world, duration, or schedule while preserving signups. Provide both Pacific date
  and time to reschedule. Rescheduling resets reminders/readiness and announces the change to that roster's players.
- `/readycheck` asks signed players to use the card's **Ready check** button, then choose **Ready**, **Running late**, or **Cannot attend**.
- At the chosen end time, the bot closes signups and removes signup roles while keeping the completed roster visible.
  `/finishraid` finishes early. Completed raids cannot be reopened, cleared, edited, or manually reminded.
- `/repairraid` repairs the card and retries failed role updates. If a missing card must be recreated, its signup message ID changes.
  `/bumproster` moves the whole interactive card and also changes its ID. Use the current card's footer for later commands.
- `/clearroster` clears an active roster. `/deleteraid` and `/deleteroster` explicitly delete a roster and its tracked posts.

Management commands require Manage Messages. Changing the signup role additionally requires Manage Roles and role hierarchy checks.
Failed role assignments/removals stay saved for automatic retry, including after resignation, clearing, role changes, or restart.
Retries check current active memberships so leaving an old raid cannot remove a role needed by a new one.

## Local Run

Use [Python 3.14.9](https://www.python.org/downloads/release/python-3149/) and a separate virtual environment.
The local runtime is installed independently at the path below; existing Python installations are unchanged.
On another machine, install that Python version first and substitute its executable path.

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python3149\python.exe" -m venv "$env:LOCALAPPDATA\RosterBot\venv-3.14"
& "$env:LOCALAPPDATA\RosterBot\venv-3.14\Scripts\python.exe" -m pip install -r requirements.txt
$env:DISCORD_TOKEN="YOUR_TOKEN_HERE"
& "$env:LOCALAPPDATA\RosterBot\venv-3.14\Scripts\python.exe" bot.py
```

Run these commands from this project directory. The source also retains Python 3.12 compatibility.
Python 3.14.9 is the validated deployment target; Python 3.15 has not been tested for this bot.

Environment variable names are documented in `.env.example` as a reference. Set them in PowerShell or the hosting panel's Environment tab;
the file is not loaded automatically. The bot requires `DISCORD_TOKEN`, while the other settings have local defaults.

Raid dates and entered times use Pacific Time (PT); posted Discord timestamps display in each viewer's timezone.
Use `today`, a month/day such as `10/24`, or an explicit year such as `2027-01-01`.
Omitting the year means the current Pacific year. Past dates and times return an error instead of moving to another year.

Standby selections stay private until **Confirm Standby** is pressed. Clear the selections and confirm to leave standby.
Selected buttons are highlighted; **Clear selections** only changes the draft until confirmed.
Compatible standby players receive a clickable **Accept / Decline** offer when a matching slot is open.
Offers expire after five minutes; accepting rechecks the vacancy and preserves your job and mount answer when compatible.
Offers survive bot restarts, and declined candidates are not repeatedly pinged for the same vacancy.
The **Mount Status** button reopens the mount question, including on locked rosters.
Existing signup posts receive updated buttons when the roster is next refreshed.
Failed role updates retry each minute while the bot can access the server.
Pending cleanup is excluded from active rosters; retry immediately using either deletion command with its signup message ID.
New rosters save their server ID so pending role cleanup can continue after the channel disappears.
Existing rosters acquire that ID when their channel is cached at startup or subsequently resolved.
Reminders mention only the players signed to that roster; standby players and other rosters are excluded.
If a saved change cannot refresh the public posts, the bot reports it instead of claiming the display is current.
Use `/repairraid` after correcting the bot's channel permissions or losing a roster post.

## bot-hosting.net

The only source files needed to run the bot are `bot.py` and `requirements.txt`.

1. Use the Python runtime; select Python 3.14 with the latest security patch if the panel offers it. Python 3.14.9 and 3.12 were tested locally; available hosting images were not inspected.
2. Upload both files to the deployment root. Repository sync is an alternative, but also copies development files.
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
On Python 3.13 and later, discord.py requires the separately installed `audioop-lts` compatibility library.

| Package | Source | License |
| --- | --- | --- |
| discord.py | [Rapptz/discord.py](https://github.com/Rapptz/discord.py) | MIT |
| aiosqlite | [omnilib/aiosqlite](https://github.com/omnilib/aiosqlite) | MIT |
| aiohttp | [aio-libs/aiohttp](https://github.com/aio-libs/aiohttp) | Apache-2.0 AND MIT |
| multidict | [aio-libs/multidict](https://github.com/aio-libs/multidict) | Apache-2.0 |
| audioop-lts | [AbstractUmbra/audioop](https://github.com/AbstractUmbra/audioop) | PSF-2.0 |

## Tests

```powershell
& "$env:LOCALAPPDATA\RosterBot\venv-3.14\Scripts\python.exe" -m unittest discover -s tests -v
```

## Discord Permissions

The bot invite should include:

- View Channels
- Send Messages
- Embed Links
- Manage Roles

Include the `bot` and `applications.commands` invite scopes. Add Send Messages in Threads when posting there.
Mention Everyone is needed only to notify nonmentionable announcement roles. The command caller must also
have Mention Everyone for that notification; otherwise choose a mentionable role. `@everyone` is rejected.
The bot edits and deletes its own posts and does not require Manage Messages.

For raid role assignment, the bot's server role must be above the raid role, such as `FFXIVActiveRoster`.
Raid roles must be ordinary member roles without moderation or server management permissions.
Dangerous channel overrides are rejected too. Use a dedicated, disposable role managed only by this bot;
cleanup removes it after a player's last roster, including if that player already held it before signing up.
If a role later becomes privileged, automatic removal stops and a server manager must review it.
Deleting a configured role never redirects its saved ID to another role with the same name.
Changing the raid role requires both Manage Messages and Manage Roles, and the caller's role must be above it
(except for the server owner).
