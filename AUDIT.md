# Follow-up review — October 25, 2026

Reviewed the complete bot, all tests, dependency metadata, and run/deployment instructions starting from commit `7c00410`.
The user authorized fixes and a newer Python runtime during the review. Changes are local; the hosted bot was not redeployed.

## Implemented product update

After the review, the user authorized the functionality and UI improvements and requested clickable job choices.

- Role selection now opens a filtered job menu, followed by clickable mount Yes/No buttons. Main Tank and Off Tank offer the
  four tank jobs, both healer slots offer four healers, melee slots offer six melee jobs, Physical Ranged offers three jobs,
  and Caster offers four. The 21 standard raid jobs were checked against the official
  [FFXIV job guide](https://na.finalfantasyxiv.com/jobguide/battle/). Limited jobs are excluded from this composition.
  Jobs and mount answers survive restart and compatible slot moves; stale or other-user pickers cannot alter a signup.
- New raids use one interactive blue-and-gold card with occupancy, jobs, mount Need/Have/Unanswered totals, duration, and
  readiness. Legacy second roster posts consolidate only after the main card updates successfully. Completed cards are read-only.
- `/editraid` preserves players while changing details or rescheduling. Rescheduling resets reminders/readiness and announces
  the change once. A duration edit cannot silently move the finish time into the past.
- Standby offers use persistent, owner-bound Accept/Decline buttons with a five-minute window. Acceptance rechecks the open
  slot, eligibility, offer token, current membership, lock, and completion state. Declines/timeouts advance the queue without
  repeatedly pinging exhausted candidates. Draft standby choices now show selection state and use “Clear selections.”
- `/readycheck` collects Ready / Running late / Cannot attend through the card. Older check generations and departed players
  cannot change current answers. `/finishraid` closes early; automatic finish uses the chosen duration and keeps the completed
  card. New raids default to three hours; migrated raids retain their earlier two-hour duration.
- Failed role updates now persist independently of active rosters, including resignation, clearing, and role changes.
  Retries recompute current active membership, preserve roles still needed by other raids, and retain both role IDs after a
  failed role change. A durable completion flag covers interruption between marking a raid completed and queuing cleanup.
- `/repairraid` recreates missing cards and retries roles. Canonical-card replacement and `/bumproster` migrate the message ID
  under old/new roster locks, roll back untracked posts on save failure, and refresh announcement links without pinging anyone.
  Failed or forbidden edits do not silently create duplicate cards. Completed-state guards prevent later reassignment or reopening.

Final validation: **175 tests pass on Python 3.14.9 and Python 3.12**; Ruff `E9,F,B,ASYNC`, `pip check`, and whitespace checks
pass. All **17 slash commands** serialize, and all **16 Python source/test files** parse using Python 3.12 syntax rules.
A read-only backup of the existing production database migrated successfully in a disposable directory, preserving every row
and original field. Its original file checksum remained unchanged. No token, live Discord server, or hosted deployment was used.
Dependencies did not change during this feature update. Runtime files, development tools, and test outputs remain outside releases.

The security review and its original 113-test checkpoint follow below; the final product update supersedes its earlier
recommendations for role retry, post repair, and automatic deletion.

## Confirmed findings and fixes

| Severity | Trigger and impact | Change |
| --- | --- | --- |
| High, conditional | A signup role can have no dangerous server permissions but grant moderation through channel overrides. Any member claiming a raid slot could receive those permissions. | Assignment now checks dangerous allow bits in all cached guild channel overrides. Ordinary private raid-channel access remains permitted. Removal also rejects roles that have become privileged and tells a manager to review them. |
| Medium | An organizer with Manage Messages but without Mention Everyone could delegate a nonmentionable announcement-role ping through the bot. | Validate explicit and default notification roles before posting or saving anything. Nonmentionable roles require the caller's channel Mention Everyone permission; `@everyone` is rejected. |
| Medium | If a configured role was deleted, name fallback could assign or remove a different role with the same name. | Saved role IDs resolve strictly by ID. Only legacy rosters without an ID retain name lookup. |
| Medium | Reminders preferred the shared signup role, notifying players from unrelated rosters and other role holders. | Manual and automatic reminders explicitly mention only that roster's signed players. Role and everyone mentions remain disabled. |
| Medium | The scheduler captured one timestamp before processing every roster and waiting on locks/Discord. An offline reproduction sent a final reminder six minutes after start. | Refresh time per roster, after acquiring its lock/loading state, and after channel resolution. Choose the current reminder window and suppress sends after start. |
| Medium | Pending role cleanup required the original channel. Deleting that channel could leave signup roles permanently assigned even while the bot still belonged to the server. | Persist `guild_id`, migrate existing schemas, backfill cached/resolved legacy channels, and resolve the server independently for pending cleanup. A confirmed channel 404 allows expired-roster cleanup; transient errors and access denials preserve active state for retry. |
| Low | `/bumproster` could resolve a roster just before deletion, then post after cleanup became pending. The subsequent rejected save did not roll the new message back. | Check pending state while holding the raid lock and roll back a newly created post on either a database error or rejected save. |
| Low | Signup, resign, standby, mount, lock/unlock, clear, and role-change paths discarded public-message refresh failures and reported unqualified success. | Preserve successful saved changes and explicitly warn when the public posts could not be refreshed. |

The eight-player signup copy now says “8-player raid.” Help and permission documentation reflect roster-specific reminders.
Removed a redundant reminder-text wrapper and unused channel parameters. The bot's documented permissions no longer include
Manage Messages or Read Message History, since it edits/deletes its own posts by known IDs. Thread posting and nonmentionable
announcement roles have separate documented permission requirements.

Discord's [permission documentation](https://github.com/discord/discord-api-docs/blob/main/developers/topics/permissions.mdx)
supports the distinction between server permissions and channel overrides. All permission and failure reproductions used mocks;
no server roles or messages were changed.

## Runtime and dependencies

- Installed the official full Windows x64 **Python 3.14.9** runtime independently under `%LOCALAPPDATA%\Programs\Python\Python3149`.
  Verified the downloaded archive against SHA-256 from Python's HTTPS runtime index. No shared Python installation, PATH,
  registration, or global package environment was changed. The [release includes security maintenance](https://www.python.org/downloads/release/python-3149/).
- Created `%LOCALAPPDATA%\RosterBot\venv-3.14` for runtime dependencies only. Development lint tooling remains in the existing
  separate audit environment. Neither runtime, dependencies, nor development tools were copied into this repository.
- Kept `discord.py 2.7.1`, `aiosqlite 0.22.1`, and patched `aiohttp 3.14.5`; upgraded compatible `multidict` to `7.2.1`.
  Pinned `audioop-lts 0.2.2` conditionally for Python 3.13+, as required by discord.py. Sources and licenses are in the README.
- All requirements installed from wheels on Windows/Python 3.14.9, and `pip check` passed. Pip is `26.2.2` in both isolated environments.
  Python 3.15 was not validated; 3.14.9 is the tested deployment target.
- Queried OSV for all 12 installed runtime dependencies plus pip: **no known matching advisories returned**. This is a
  point-in-time database result, not proof of absence of vulnerabilities. Runtime provenance and dependency results are saved
  under `%LOCALAPPDATA%\RosterBot\verification`, outside source and release artifacts.

## Verification

- **113 tests passed** on Python 3.14.9 and on the existing Python 3.12 audit environment using the updated requirements.
  The previous suite had 87 tests. New regressions cover channel overrides, ping authorization, strict role IDs, scheduler delays,
  missing-channel cleanup, legacy server-ID backfill, bump rollback, and refresh-failure reporting.
- Ruff `E9,F,B,ASYNC`, `git diff --check`, and dependency consistency checks passed. Reviewed the final changes independently
  for redundant logic and regressions; kept the runtime changes within existing bot responsibilities.
- Serialized all **13 slash commands** on Python 3.14.9. Guilds remains the only Gateway intent, and default mentions remain disabled.
- Parsed all 11 Python source/test files with Python 3.12 syntax rules. High-confidence token-pattern scanning found no matches
  in current project source/configuration; this was not a full historical or provider-side secret audit.
- Opened the existing production database read-only: SQLite `quick_check` returned `ok`, and its file checksum remained unchanged.
  Schema migrations and persistence tests used disposable databases. A lifecycle fixture was updated to mock the new backfill
  call after an initial read query encountered the old schema; that query did not migrate or write the production database.

The hosted environment, live Discord delivery, permissions, reconnects, and rate limiting were not exercised. Public hosting
documentation confirms dependency installation from `requirements.txt`, but available runtime images in the account were not inspected.
Back up the deployment database with the bot stopped, upload only `bot.py` and `requirements.txt`, retain the database, install
requirements, select a supported patched Python runtime, and restart to apply these changes. No release package was generated.

## Remaining constraints

- Use a **dedicated, disposable signup role** owned operationally by this bot. There is no durable record of whether a member
  already held a safe role before signup, so final cleanup removes that role too. Supporting general-purpose roles safely needs
  ownership tracking shared across all rosters, rather than a per-roster flag.
- Failed role updates retry automatically; persistent permission problems still require a manager to correct the server configuration.
- Missing public posts require `/repairraid`; the scheduler does not automatically recreate missing cards.
- An old roster whose channel vanished before its server ID could be learned may still need manual cleanup. If the bot loses
  server access entirely, it cannot remove server roles; saved pending cleanup remains for a later attempt.
- Reminder delivery and its database acknowledgment are separate operations; a crash between them can duplicate a ping.
  Discord may delay an already-issued send internally after the last local clock check.
- Run one bot instance per database. The roster and role locks do not coordinate multiple processes.
- Automatic finishing retains completed cards and their player data until an organizer explicitly deletes them.

## Further ideas

Reusable event-goal/composition templates, a manager raid selector, and optional original branding artwork remain possible
follow-ups. The accepted editing, standby, repair, readiness, finishing, job, and roster-card improvements are implemented above.

---

# Previous code audit — October 24, 2026

Reviewed `bot.py`, the existing tests, dependency metadata, and deployment configuration.
Implemented the following fixes; the production database was inspected read-only and was not migrated or changed.

| Area | Findings and changes |
| --- | --- |
| Role authorization | `/raidrole` now requires Manage Messages and Manage Roles, respects the caller's role hierarchy, and rejects privileged signup roles. Default and legacy roles receive the same safety check when assigned. |
| Shared roles | Leaving, clearing, changing, or deleting a raid preserves roles needed by another roster. Member/role locks serialize cross-raid operations, and duplicate role names are resolved by effective ID. |
| Concurrent changes | Setup holds the raid lock through persistence and button activation. Stale standby pickers respect locked rosters. Deletion marks saved membership as pending cleanup before checking shared-role removal. |
| Discord failures | Role assignment and member lookup failures produce explicit warnings. Partial post deletion keeps the saved roster for another attempt. Failed role cleanup after deletion retains the roster and retries each minute, including after restart. Pending rows cannot be overwritten by stale roster saves. Database failures during setup roll back unsaved posts. |
| Discord traffic | Known message IDs use partial messages, removing the GET before each edit/delete. Only the guilds Gateway intent is enabled; notification recipients are explicit. |
| Database work | Latest-channel lookup uses one indexed query. Scheduled lookup uses an index and excludes distant raids. Idle scheduler entries avoid extra reads, writes, and channel requests. Named rows replace repeated positional projections and retired-field reads. |
| Lifecycle | Startup setup runs once, persistent views stop when their raids are deleted, unused locks are reclaimed, and shutdown cancels and awaits the scheduler before closing Discord. |
| Reminders | Final reminders suppress stale automatic reminders. Relative timestamps reflect the actual scheduled time after delays. One raid's failure does not prevent others from being processed. |
| Input and rendering | Reproduced the original parser moving `10/24` to next year at a UTC date boundary. Yearless dates now always use the current Pacific year; past schedules return an error. `today` is supported and Pacific time is explicit in command descriptions. Tests cover a raid five minutes away while UTC is on the following day. Fixed leap-day parsing, skipped/repeated DST hours, malformed saved values, oversized lines, and embed field bounds. Message IDs are validated before SQLite binding. |
| Standby and mounts | Standby toggles and Clear edit a private draft; Confirm alone commits it, preserving the mount answer. Abandoned pickers leave saved entries unchanged. A persistent Mount Status button reopens or corrects an answer, including on locked rosters. |
| Maintenance | Removed redundant checks and unused roster metadata; shared the deletion command handler while retaining both command names. Corrected help for presets, role permissions, confirmation, and cleanup. Added searchable sections and environment/dependency instructions. No third-party implementation was added to project source. |

The existing environment contained `aiohttp 3.13.5` and `multidict 6.7.1`, which matched published advisories.
`requirements.txt` now pins `aiohttp 3.14.5` and `multidict 6.9.1`, alongside the existing Discord and SQLite libraries.
Examples of the upstream advisories: [aiohttp WebSocket memory limits](https://github.com/aio-libs/aiohttp/security/advisories/GHSA-xcgm-r5h9-7989)
and [multidict reference leak](https://github.com/aio-libs/multidict/security/advisories/GHSA-54p9-h82j-f925).

Verification used an isolated Windows Python 3.12 environment outside the project and shared Python installations:

- All 87 unit and regression tests passed, including temporary-database migrations, concurrency, rollback, permissions, date boundaries, standby drafts, mount edits, and cleanup recovery after restart. The original suite contained 13 tests.
- Ruff checks `E9,F,B,ASYNC`, `pip check`, and `git diff --check` passed.
- Python 3.12 syntax parsing and serialization of all 13 slash commands passed.
- SQLite query plans confirmed both lookup indexes are used. The production database's read-only quick integrity check returned `ok`.
- An OSV query for the 12 installed runtime packages in the patched environment returned no known matching advisories. This is a point-in-time database check, not a guarantee against vulnerabilities.

Run the bot as a single instance against its SQLite database; its locks coordinate tasks within one process.
Install the updated requirements before restarting or redeploying. Dependency sources, licenses, and installation commands are in `README.md`.

Live Discord permissions, notification delivery, rate limits, reconnect behavior, and the hosted deployment were not exercised.
Tests use mocked Discord operations; no token was used and no server messages or roles were changed.
The timezone helpers implement US rules in effect since 2007. Role-removal failures during resigning or clearing an active roster are reported for manual correction; durable background retry applies to deletion cleanup.
Applying these source changes requires updating and restarting or redeploying the running bot. Previously saved wrong-year raids are not automatically rescheduled.
Reminder delivery and its database acknowledgment are separate operations, so a crash between them can still produce a duplicate notification.
No production load benchmark was run; performance improvements are supported by request counts, scheduler assertions, and query plans.

Hosting configuration now targets bot-hosting.net. Removed the unused buildpack runtime selector and deployment/startup files.
The Python bot already uses the host's supported environment variables and dependency installation, so no runtime adapter or new dependency was needed.
The README documents the entry file, minimal upload files, and database-preserving GitHub sync settings from the official hosting guides.
