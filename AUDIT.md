# Code audit — October 24, 2026

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
