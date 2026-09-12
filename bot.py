import ast
import asyncio
import json
import logging
import os
import random
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path

import aiosqlite
import discord
from discord import app_commands


intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("rosterbot")

DB_PATH = str(Path(os.getenv("ROSTERBOT_DB_PATH", "raids.db")).expanduser())
views_loaded = False
reminder_task = None
raid_locks: dict[int, asyncio.Lock] = {}
DEFAULT_RAID_ROLE_NAME = "FFXIVActiveRoster"
DEFAULT_NOTIFY_ROLE_NAME = "FFXIV Raid"
DEFAULT_TIMEZONE_NAME = "PT"

STANDBY_CHOICES = ["Tank", "DPS", "Healer"]

PRESET_CHOICES = [
    app_commands.Choice(name="Everkeep EX", value="Everkeep EX"),
    app_commands.Choice(name="M1S", value="AAC Light-heavyweight M1 Savage"),
    app_commands.Choice(name="M2S", value="AAC Light-heavyweight M2 Savage"),
    app_commands.Choice(name="M3S", value="AAC Light-heavyweight M3 Savage"),
    app_commands.Choice(name="M4S", value="AAC Light-heavyweight M4 Savage"),
    app_commands.Choice(name="FRU", value="Futures Rewritten Ultimate"),
]

STANDARD_OFFSETS = {
    "ET": -5,
    "CT": -6,
    "MT": -7,
    "PT": -8,
    "UTC": 0,
}

DST_OFFSETS = {
    "ET": -4,
    "CT": -5,
    "MT": -6,
    "PT": -7,
}

ROLES = [
    ("MT", "Main Tank", "🛡️", discord.ButtonStyle.primary, "Tank"),
    ("OT", "Off Tank", "🛡️", discord.ButtonStyle.primary, "Tank"),
    ("H1", "Healer 1", "✨", discord.ButtonStyle.success, "Healer"),
    ("H2", "Healer 2", "✨", discord.ButtonStyle.success, "Healer"),
    ("M1", "Melee 1", "⚔️", discord.ButtonStyle.danger, "DPS"),
    ("M2", "Melee 2", "⚔️", discord.ButtonStyle.danger, "DPS"),
    ("R1", "Ranged 1", "🏹", discord.ButtonStyle.danger, "DPS"),
    ("R2", "Ranged 2", "🏹", discord.ButtonStyle.danger, "DPS"),
]

ROLE_LABELS = {key: label for key, label, _, _, _ in ROLES}
ROSTER_GROUPS = [
    ("Tanks", ("MT", "OT"), 2),
    ("Healers", ("H1", "H2"), 2),
    ("Melee DPS", ("M1", "M2"), 2),
    ("Ranged DPS", ("R1", "R2"), 2),
]
REMINDER_WINDOWS = [
    (24 * 60 * 60, "24 hours"),
    (12 * 60 * 60, "12 hours"),
    (8 * 60 * 60, "8 hours"),
    (6 * 60 * 60, "6 hours"),
]
FIVE_MINUTE_REMINDER_SECONDS = 5 * 60
REMINDER_SELECTION_GRACE_SECONDS = 60
FIVE_MINUTE_MESSAGES = [
    "Happy mount farming!",
    "Let's get that clear and that shiny prize!",
    "Good vibes, clean pulls, big loot!",
    "Bring your snacks and your best rotations!",
    "May the drops be kind and the mechanics be readable!",
    "Time to make this boss regret the queue pop!",
    "You've got this. Let's make it a smooth run!",
    "Mount luck is officially being summoned!",
]


def encode_json(value) -> str:
    return json.dumps(value, separators=(",", ":"))


def decode_json(raw: str | None, fallback):
    if not raw:
        return fallback

    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        try:
            value = ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            return fallback

    return value if isinstance(value, type(fallback)) else fallback


def decode_signups(raw: str | None) -> dict:
    value = decode_json(raw, {})
    normalized = {}
    for role_key, player in value.items():
        if isinstance(player, dict):
            normalized[role_key] = {
                "user_id": player.get("user_id"),
                "display_name": player.get("display_name", "Unknown"),
                "mention": player.get("mention", player.get("display_name", "Unknown")),
                "needs_mount": player.get("needs_mount"),
            }
        else:
            normalized[role_key] = {
                "user_id": None,
                "display_name": str(player),
                "mention": str(player),
                "needs_mount": None,
            }
    return normalized


def decode_standby(raw: str | None) -> dict:
    value = decode_json(raw, {})
    standby = {}
    for user_id, entry in value.items():
        if not isinstance(entry, dict):
            continue

        roles = [role for role in entry.get("roles", []) if role in STANDBY_CHOICES]
        if not roles:
            continue

        standby[str(user_id)] = {
            "user_id": entry.get("user_id"),
            "display_name": entry.get("display_name", "Unknown"),
            "mention": entry.get("mention", entry.get("display_name", "Unknown")),
            "roles": roles,
            "needs_mount": entry.get("needs_mount"),
        }
    return standby


def decode_reminders(raw: str | None) -> dict:
    value = decode_json(raw, {})
    return {str(key): bool(sent) for key, sent in value.items()}


def player_from_user(user: discord.abc.User) -> dict:
    return {
        "user_id": user.id,
        "display_name": user.display_name,
        "mention": user.mention,
        "needs_mount": None,
    }


def player_text(player: dict | None) -> str:
    if not player:
        return "OPEN"
    return player.get("mention") or player.get("display_name") or "Unknown"


def mount_text(player: dict | None) -> str:
    if not player or player.get("needs_mount") is None:
        return "Mount Unknown"

    return "Needs Mount" if player.get("needs_mount") else "Doesn't Need Mount"


def roster_player_text(player: dict | None) -> str:
    if not player:
        return "OPEN"

    return f"{player_text(player)} - {mount_text(player)}"


def user_is_signed_up(raid: dict, user_id: int) -> bool:
    return any(player.get("user_id") == user_id for player in raid["signups"].values())


def remove_user_from_standby(standby: dict, user_id: int) -> bool:
    return standby.pop(str(user_id), None) is not None


def standby_role_text(roles: list[str]) -> str:
    if set(roles) == set(STANDBY_CHOICES):
        return "Any"

    return "/".join(role for role in STANDBY_CHOICES if role in roles)


def mount_needed_count(raid: dict) -> int:
    main_count = sum(1 for player in raid["signups"].values() if player.get("needs_mount") is True)
    standby_count = sum(1 for player in raid["standby"].values() if player.get("needs_mount") is True)
    return main_count + standby_count


def missing_role_labels(signups: dict) -> list[str]:
    return [label for role_key, label, _, _, _ in ROLES if role_key not in signups]


def fit_embed_lines(lines: list[str], empty_text: str) -> str:
    if not lines:
        return empty_text

    visible_lines = []
    for line in lines:
        remaining = len(lines) - len(visible_lines) - 1
        suffix = f"\n... and {remaining} more." if remaining else ""
        candidate = "\n".join([*visible_lines, line]) + suffix
        if len(candidate) > 1024:
            break
        visible_lines.append(line)

    remaining = len(lines) - len(visible_lines)
    suffix = f"\n... and {remaining} more." if remaining else ""
    while visible_lines and len("\n".join(visible_lines) + suffix) > 1024:
        visible_lines.pop()
        remaining += 1
        suffix = f"\n... and {remaining} more."

    return "\n".join(visible_lines) + suffix


def chunk_text_lines(text: str, max_length: int = 1900) -> list[str]:
    chunks = []
    current_lines = []
    current_length = 0
    for line in text.splitlines():
        added_length = len(line) + (1 if current_lines else 0)
        if current_lines and current_length + added_length > max_length:
            chunks.append("\n".join(current_lines))
            current_lines = []
            current_length = 0

        current_lines.append(line)
        current_length += len(line) + (1 if len(current_lines) > 1 else 0)

    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks or [""]


def signed_player_mentions(signups: dict) -> str:
    mentions = [player_text(player) for player in signups.values()]
    return " ".join(mentions) if mentions else "No one is signed up yet."


def find_role_by_name(guild: discord.Guild | None, role_name: str | None):
    if guild is None or not role_name:
        return None

    return discord.utils.get(guild.roles, name=role_name)


def raid_role_for_guild(guild: discord.Guild | None, raid: dict):
    if guild is None:
        return None

    role_id = raid.get("raid_role_id")
    if role_id:
        role = guild.get_role(int(role_id))
        if role:
            return role

    return find_role_by_name(guild, raid.get("raid_role_name") or DEFAULT_RAID_ROLE_NAME)


def get_raid_lock(signup_message_id: int) -> asyncio.Lock:
    return raid_locks.setdefault(signup_message_id, asyncio.Lock())


def allowed_mentions_for_raid(channel, raid: dict) -> discord.AllowedMentions:
    guild = getattr(channel, "guild", None)
    role = raid_role_for_guild(guild, raid)
    users = []
    for player in raid["signups"].values():
        user_id = player.get("user_id")
        if user_id:
            users.append(discord.Object(id=int(user_id)))

    return discord.AllowedMentions(
        everyone=False,
        roles=[role] if role else False,
        users=users,
        replied_user=False,
    )


async def add_raid_role(member: discord.Member, raid: dict) -> str | None:
    role = raid_role_for_guild(member.guild, raid)
    if role is None:
        return f"I could not find a Discord role named `{raid.get('raid_role_name') or DEFAULT_RAID_ROLE_NAME}`."

    if role in member.roles:
        return None

    try:
        await member.add_roles(role, reason=f"Signed up for {raid_title(raid)}")
    except discord.Forbidden:
        return "I could not assign the raid role. Make sure I have Manage Roles and my bot role is above that role."

    return None


async def remove_raid_role(member: discord.Member, raid: dict) -> str | None:
    role = raid_role_for_guild(member.guild, raid)
    if role is None or role not in member.roles:
        return None

    try:
        await member.remove_roles(role, reason=f"Left {raid_title(raid)}")
    except discord.Forbidden:
        return "I could not remove the raid role. Make sure I have Manage Roles and my bot role is above that role."
    except discord.HTTPException:
        return "Discord could not remove the raid role right now. Please try again."

    return None


async def fetch_guild_member(guild: discord.Guild | None, user_id: int | None) -> discord.Member | None:
    if guild is None or not user_id:
        return None

    member = guild.get_member(int(user_id))
    if member is None:
        try:
            member = await guild.fetch_member(int(user_id))
        except discord.NotFound:
            return None
        except discord.Forbidden:
            return None
        except discord.HTTPException:
            logger.exception("Could not fetch guild member %s", user_id)
            return None

    return member


async def remove_raid_role_from_player(
    guild: discord.Guild | None,
    raid: dict,
    player: dict,
) -> str | None:
    member = await fetch_guild_member(guild, player.get("user_id"))
    if member is None:
        return None

    return await remove_raid_role(member, raid)


async def remove_all_raid_roles(guild: discord.Guild | None, raid: dict) -> list[str]:
    warnings = []
    for player in raid["signups"].values():
        warning = await remove_raid_role_from_player(guild, raid, player)
        if warning and warning not in warnings:
            warnings.append(warning)
    return warnings


def reminder_ping_text(channel, raid: dict) -> str:
    guild = getattr(channel, "guild", None)
    role = raid_role_for_guild(guild, raid)
    if role:
        return role.mention

    return signed_player_mentions(raid["signups"])


def five_minute_reminder_text(channel, raid: dict) -> str:
    ping_text = reminder_ping_text(channel, raid)
    cheerful_message = random.choice(FIVE_MINUTE_MESSAGES)
    if raid.get("meeting_server"):
        return (
            f"{ping_text}, there are 5 minutes remaining until **{raid_title(raid)}** starts! "
            f"We are gathering in **{raid['meeting_server']}**! {cheerful_message}"
        )

    return (
        f"{ping_text}, there are 5 minutes remaining until **{raid_title(raid)}** starts! "
        f"Join the VC to figure out where the raid group is meeting! {cheerful_message}"
    )


class ErrorHandledView(discord.ui.View):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.error(
            "Component interaction failed for %s",
            getattr(item, "custom_id", type(item).__name__),
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "Something went wrong while updating the roster. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            logger.exception("Could not report a component interaction error")


def require_roster_manager(interaction: discord.Interaction) -> tuple[bool, str | None]:
    if not isinstance(interaction.user, discord.Member):
        return False, "I could not confirm your server permissions."

    permissions = interaction.user.guild_permissions
    if permissions.manage_messages or permissions.administrator:
        return True, None

    return False, "You need Manage Messages permission to manage raid rosters."


async def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS signups (
                signup_message_id INTEGER PRIMARY KEY,
                roster_message_id INTEGER,
                channel_id INTEGER,
                raid_name TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                signups TEXT NOT NULL DEFAULT '{}',
                waitlist TEXT NOT NULL DEFAULT '{}',
                standby TEXT NOT NULL DEFAULT '{}',
                confirmations TEXT NOT NULL DEFAULT '[]',
                scheduled_at INTEGER,
                timezone TEXT,
                locked INTEGER NOT NULL DEFAULT 0,
                role_restrictions INTEGER NOT NULL DEFAULT 0,
                group_name TEXT,
                reminders_sent TEXT NOT NULL DEFAULT '{}',
                reminder_offset INTEGER,
                raid_role_id INTEGER,
                raid_role_name TEXT,
                announcement_message_id INTEGER,
                setup_message TEXT,
                notify_role_id INTEGER,
                notify_role_name TEXT,
                meeting_server TEXT
            )
            """
        )

        async with db.execute("PRAGMA table_info(signups)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}

        if "message_id" in columns and "signup_message_id" not in columns:
            await db.execute("ALTER TABLE signups RENAME COLUMN message_id TO signup_message_id")
            columns.remove("message_id")
            columns.add("signup_message_id")

        migrations = (
            ("roster_message_id", "INTEGER"),
            ("channel_id", "INTEGER"),
            ("scheduled_at", "INTEGER"),
            ("timezone", "TEXT"),
            ("waitlist", "TEXT NOT NULL DEFAULT '{}'"),
            ("standby", "TEXT NOT NULL DEFAULT '{}'"),
            ("confirmations", "TEXT NOT NULL DEFAULT '[]'"),
            ("locked", "INTEGER NOT NULL DEFAULT 0"),
            ("role_restrictions", "INTEGER NOT NULL DEFAULT 0"),
            ("group_name", "TEXT"),
            ("reminders_sent", "TEXT NOT NULL DEFAULT '{}'"),
            ("reminder_offset", "INTEGER"),
            ("raid_role_id", "INTEGER"),
            ("raid_role_name", "TEXT"),
            ("announcement_message_id", "INTEGER"),
            ("setup_message", "TEXT"),
            ("notify_role_id", "INTEGER"),
            ("notify_role_name", "TEXT"),
            ("meeting_server", "TEXT"),
        )
        for column_name, column_type in migrations:
            if column_name not in columns:
                await db.execute(f"ALTER TABLE signups ADD COLUMN {column_name} {column_type}")

        # Role restrictions were removed from the public bot interface. Keep the
        # legacy column for database compatibility, but disable old saved values.
        await db.execute("UPDATE signups SET role_restrictions = 0 WHERE role_restrictions != 0")
        await db.commit()


def raid_from_row(row) -> dict | None:
    if not row:
        return None

    return {
        "signup_message_id": row[0],
        "roster_message_id": row[1],
        "channel_id": row[2],
        "raid_name": row[3],
        "date": row[4],
        "time": row[5],
        "signups": decode_signups(row[6]),
        "standby": decode_standby(row[8]),
        "scheduled_at": row[10],
        "timezone": row[11],
        "locked": bool(row[12]),
        "group_name": row[14],
        "reminders_sent": decode_reminders(row[15]),
        "reminder_offset": row[16],
        "raid_role_id": row[17],
        "raid_role_name": row[18],
        "announcement_message_id": row[19],
        "setup_message": row[20],
        "notify_role_id": row[21],
        "notify_role_name": row[22],
        "meeting_server": row[23],
    }


async def fetch_signup(signup_message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT signup_message_id, roster_message_id, channel_id, raid_name, date, time,
                   signups, waitlist, standby, confirmations, scheduled_at, timezone, locked,
                   role_restrictions, group_name, reminders_sent, reminder_offset,
                   raid_role_id, raid_role_name, announcement_message_id, setup_message,
                   notify_role_id, notify_role_name, meeting_server
            FROM signups
            WHERE signup_message_id = ?
            """,
            (signup_message_id,),
        ) as cursor:
            return raid_from_row(await cursor.fetchone())


async def fetch_latest_signup_for_channel(channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT signup_message_id
            FROM signups
            WHERE channel_id = ?
            ORDER BY signup_message_id DESC
            LIMIT 1
            """,
            (channel_id,),
        ) as cursor:
            row = await cursor.fetchone()

    return await fetch_signup(row[0]) if row else None


async def fetch_scheduled_raids():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT signup_message_id, roster_message_id, channel_id, raid_name, date, time,
                   signups, waitlist, standby, confirmations, scheduled_at, timezone, locked,
                   role_restrictions, group_name, reminders_sent, reminder_offset,
                   raid_role_id, raid_role_name, announcement_message_id, setup_message,
                   notify_role_id, notify_role_name, meeting_server
            FROM signups
            WHERE scheduled_at IS NOT NULL
            """,
        ) as cursor:
            return [raid_from_row(row) for row in await cursor.fetchall()]


async def save_raid_state(raid: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE signups
            SET roster_message_id = ?, signups = ?, standby = ?, locked = ?,
                group_name = ?, reminders_sent = ?, reminder_offset = ?,
                raid_role_id = ?, raid_role_name = ?, announcement_message_id = ?,
                setup_message = ?, notify_role_id = ?, notify_role_name = ?, meeting_server = ?
            WHERE signup_message_id = ?
            """,
            (
                raid.get("roster_message_id"),
                encode_json(raid["signups"]),
                encode_json(raid["standby"]),
                int(raid["locked"]),
                raid.get("group_name"),
                encode_json(raid["reminders_sent"]),
                raid.get("reminder_offset"),
                raid.get("raid_role_id"),
                raid.get("raid_role_name"),
                raid.get("announcement_message_id"),
                raid.get("setup_message"),
                raid.get("notify_role_id"),
                raid.get("notify_role_name"),
                raid.get("meeting_server"),
                raid["signup_message_id"],
            ),
        )
        await db.commit()


async def save_reminder_state(raid: dict):
    """Save scheduler fields without overwriting newer roster selections."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE signups
            SET reminders_sent = ?, reminder_offset = ?
            WHERE signup_message_id = ?
            """,
            (
                encode_json(raid["reminders_sent"]),
                raid.get("reminder_offset"),
                raid["signup_message_id"],
            ),
        )
        await db.commit()


async def delete_raid_row(signup_message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM signups WHERE signup_message_id = ?", (signup_message_id,))
        await db.commit()


def first_sunday(year: int, month: int):
    first_day = datetime(year, month, 1).date()
    days_until_sunday = (6 - first_day.weekday()) % 7
    return first_day + timedelta(days=days_until_sunday)


def second_sunday(year: int, month: int):
    return first_sunday(year, month) + timedelta(days=7)


def is_us_daylight_time(local_datetime: datetime) -> bool:
    year = local_datetime.year
    dst_start = datetime.combine(second_sunday(year, 3), datetime_time(hour=2))
    dst_end = datetime.combine(first_sunday(year, 11), datetime_time(hour=2))
    return dst_start <= local_datetime < dst_end


def timezone_for_local_datetime(timezone_name: str, local_datetime: datetime):
    if timezone_name not in STANDARD_OFFSETS:
        raise ValueError("That timezone is not available.")

    offset_hours = STANDARD_OFFSETS[timezone_name]
    if timezone_name in DST_OFFSETS and is_us_daylight_time(local_datetime):
        offset_hours = DST_OFFSETS[timezone_name]

    return timezone(timedelta(hours=offset_hours), name=timezone_name)


def parse_raid_date(date_text: str):
    clean_date = " ".join(date_text.strip().replace("-", "/").split())
    current_year = datetime.now().year

    date_formats_with_year = (
        "%Y/%m/%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%B %d %Y",
        "%b %d %Y",
        "%B %d, %Y",
        "%b %d, %Y",
    )
    for date_format in date_formats_with_year:
        try:
            return datetime.strptime(clean_date, date_format).date()
        except ValueError:
            pass

    date_formats_without_year = (
        "%m/%d",
        "%B %d",
        "%b %d",
        "%B %d,",
        "%b %d,",
    )
    for date_format in date_formats_without_year:
        try:
            parsed = datetime.strptime(clean_date, date_format).date()
            parsed = parsed.replace(year=current_year)
            if parsed < datetime.now().date():
                parsed = parsed.replace(year=current_year + 1)
            return parsed
        except ValueError:
            pass

    raise ValueError("Use a date like `5/25`, `May 25`, or `2026-05-25`.")


def parse_raid_datetime(date_text: str, time_text: str, timezone_name: str) -> int:
    parsed_date = parse_raid_date(date_text)

    clean_time = " ".join(time_text.strip().upper().replace(".", "").split())
    if clean_time.endswith("AM") or clean_time.endswith("PM"):
        suffix = clean_time[-2:]
        prefix = clean_time[:-2].strip()
        clean_time = f"{prefix} {suffix}"

    time_formats = ("%H:%M", "%I:%M %p", "%I %p")
    parsed_time = None
    for time_format in time_formats:
        try:
            parsed_time = datetime.strptime(clean_time, time_format).time()
            break
        except ValueError:
            pass

    if parsed_time is None:
        raise ValueError("Use a time like `20:00`, `8:00 PM`, `8PM`, or `8 PM`.") from None

    local_datetime = datetime.combine(parsed_date, parsed_time)
    selected_timezone = timezone_for_local_datetime(timezone_name, local_datetime)
    scheduled_datetime = local_datetime.replace(tzinfo=selected_timezone)
    return int(scheduled_datetime.timestamp())


def schedule_text(raid: dict) -> str:
    meeting_text = f"\n**Meeting Server:** {raid['meeting_server']}" if raid.get("meeting_server") else ""
    if raid.get("scheduled_at"):
        return f"**When:** <t:{raid['scheduled_at']}:F>\n**Countdown:** <t:{raid['scheduled_at']}:R>{meeting_text}"

    return f"**Date:** {raid['date']}\n**Gather Time:** {raid['time']}{meeting_text}"


def choose_reminder_offset(scheduled_at: int, now: int | None = None) -> int | None:
    current_timestamp = now if now is not None else int(datetime.now(timezone.utc).timestamp())
    seconds_until_raid = scheduled_at - current_timestamp
    for seconds_before, _ in REMINDER_WINDOWS:
        if seconds_until_raid + REMINDER_SELECTION_GRACE_SECONDS >= seconds_before:
            return seconds_before
    return None


def reminder_label(seconds_before: int | None) -> str:
    for seconds, label in REMINDER_WINDOWS:
        if seconds == seconds_before:
            return label
    return "disabled"


def raid_title(raid: dict) -> str:
    group = f" [{raid['group_name']}]" if raid.get("group_name") else ""
    return f"{raid['raid_name']}{group}"


def default_setup_message(raid_name: str) -> str:
    return (
        f"Hey guys! This weekend will be **{raid_name}**! Please look at the appropriate forum channel "
        "for a guide or go in blind! Pick your role below and let's get this mount!"
    )


def clean_setup_message(message: str | None) -> str | None:
    if not message:
        return None

    cleaned = " ".join(message.strip().split())
    return cleaned[:1500] if cleaned else None


def clean_short_text(value: str | None, field_name: str, max_length: int) -> str | None:
    if not value:
        return None

    cleaned = " ".join(value.strip().split())
    if len(cleaned) > max_length:
        raise ValueError(f"{field_name} must be {max_length} characters or fewer.")
    return cleaned or None


def signup_embed(raid: dict) -> discord.Embed:
    status = []
    if raid["locked"]:
        status.append("Roster locked")
    if raid.get("reminder_offset"):
        status.append(f"{reminder_label(raid['reminder_offset'])} reminder")
    if raid.get("raid_role_name"):
        status.append(f"{raid['raid_role_name']} active roster role")

    status_text = f"\n**Status:** {', '.join(status)}" if status else ""
    embed = discord.Embed(
        title=f"⚔️ {raid_title(raid)}",
        description=(
            f"{schedule_text(raid)}{status_text}\n\n"
            "Choose your light party role below."
        ),
        color=0x3E6AE1,
    )
    embed.set_footer(text=f"Signup message ID: {raid['signup_message_id']}")
    return embed


def roster_embed(raid: dict) -> discord.Embed:
    missing = missing_role_labels(raid["signups"])
    summary = (
        f"{schedule_text(raid)}\n"
        f"**Standby:** {len(raid['standby'])}\n"
        f"**Open:** {len(missing)}"
    )
    embed = discord.Embed(
        title=f"📜 {raid_title(raid)} Roster",
        description=summary,
        color=0xD4AF37,
    )

    tank_lines = []
    healer_lines = []
    dps_lines = []
    for role_key, role_label, role_emoji, _, _ in ROLES:
        player = raid["signups"].get(role_key)
        line = f"{role_emoji} **{role_label}:** {roster_player_text(player)}"

        if role_key in {"MT", "OT"}:
            tank_lines.append(line)
        elif role_key in {"H1", "H2"}:
            healer_lines.append(line)
        else:
            dps_lines.append(line)

    embed.add_field(name="__**Tanks**__", value="\n".join(tank_lines), inline=False)
    embed.add_field(name="__**Healers**__", value="\n".join(healer_lines), inline=False)
    embed.add_field(name="__**DPS**__", value="\n".join(dps_lines), inline=False)
    open_slot_lines = []
    for _, role_keys, _ in ROSTER_GROUPS:
        group_slots = []
        for role_key in role_keys:
            role_label = ROLE_LABELS[role_key]
            group_slots.append(f"~~{role_label}~~" if role_key in raid["signups"] else role_label)
        open_slot_lines.append(", ".join(group_slots))

    embed.add_field(
        name="__**Open Slots**__",
        value="\n".join(open_slot_lines),
        inline=False,
    )
    standby_lines = [
        f"{player_text(entry)} - {standby_role_text(entry['roles'])} - {mount_text(entry)}"
        for entry in raid["standby"].values()
    ]
    embed.add_field(
        name="__**Standby**__",
        value=fit_embed_lines(standby_lines, "No standby players."),
        inline=False,
    )
    embed.add_field(name="__**Mounts Needed**__", value=str(mount_needed_count(raid)), inline=False)
    return embed


def roster_export_text(raid: dict) -> str:
    lines = [f"{raid_title(raid)}", f"When: <t:{raid['scheduled_at']}:F>" if raid.get("scheduled_at") else ""]
    for role_key, role_label, _, _, _ in ROLES:
        player = raid["signups"].get(role_key)
        lines.append(f"{role_label}: {roster_player_text(player)}")
    missing = missing_role_labels(raid["signups"])
    lines.append(f"Missing: {', '.join(missing) if missing else 'None'}")
    if raid["standby"]:
        lines.append("Standby:")
        for entry in raid["standby"].values():
            lines.append(f"{player_text(entry)} - {standby_role_text(entry['roles'])} - {mount_text(entry)}")
    lines.append(f"Mounts Needed: {mount_needed_count(raid)}")
    return "\n".join(line for line in lines if line)


async def update_raid_messages(channel, raid: dict):
    if channel is None:
        return False

    view = RaidSignupView(raid)
    updated = True

    try:
        signup_message = await channel.fetch_message(raid["signup_message_id"])
        await signup_message.edit(embed=signup_embed(raid), view=view)
    except discord.NotFound:
        logger.warning("Signup message %s no longer exists", raid["signup_message_id"])
        updated = False
    except discord.HTTPException:
        logger.exception("Could not update signup message %s", raid["signup_message_id"])
        updated = False

    roster_message_id = raid.get("roster_message_id")
    if not roster_message_id:
        return updated

    try:
        roster_message = await channel.fetch_message(roster_message_id)
        await roster_message.edit(embed=roster_embed(raid))
    except discord.NotFound:
        logger.warning("Roster message %s no longer exists", roster_message_id)
        updated = False
    except discord.HTTPException:
        logger.exception("Could not update roster message %s", roster_message_id)
        updated = False

    return updated


async def fetch_raid_channel(raid: dict):
    channel = bot.get_channel(raid["channel_id"])
    if channel is not None:
        return channel

    try:
        return await bot.fetch_channel(raid["channel_id"])
    except discord.HTTPException:
        return None


async def delete_raid_messages(channel, raid: dict):
    if channel is None:
        return False

    deleted = True
    for message_id in (
        raid.get("announcement_message_id"),
        raid["signup_message_id"],
        raid.get("roster_message_id"),
    ):
        if not message_id:
            continue
        try:
            message = await channel.fetch_message(message_id)
            await message.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            logger.exception("Could not delete raid message %s", message_id)
            deleted = False

    return deleted


async def delete_created_messages(messages: list[discord.Message], reason: str) -> None:
    for message in reversed(messages):
        try:
            await message.delete()
        except discord.HTTPException:
            logger.exception("Could not roll back message %s after %s", message.id, reason)


async def resolve_raid_for_command(interaction: discord.Interaction, signup_message_id: str | None):
    if interaction.channel is None:
        return None, "This command needs to be used in a server channel."

    if signup_message_id:
        try:
            raid = await fetch_signup(int(signup_message_id.strip()))
        except ValueError:
            return None, "That signup message ID does not look valid."
    else:
        raid = await fetch_latest_signup_for_channel(interaction.channel_id)

    if not raid:
        return None, "I could not find a raid signup."

    if raid.get("channel_id") and raid["channel_id"] != interaction.channel_id:
        return None, "That signup belongs to a different channel."

    return raid, None


def reset_roster_state(raid: dict) -> None:
    raid["signups"] = {}
    raid["standby"] = {}
    raid["locked"] = False


async def delete_raid_for_command(interaction: discord.Interaction, raid: dict) -> tuple[bool, list[str]]:
    async with get_raid_lock(raid["signup_message_id"]):
        current_raid = await fetch_signup(raid["signup_message_id"])
        if not current_raid:
            return False, ["That roster was already deleted."]

        warnings = await remove_all_raid_roles(interaction.guild, current_raid)
        if not await delete_raid_messages(interaction.channel, current_raid):
            warnings.append("I could not delete every bot post, so the saved roster was kept for another attempt.")
            return False, warnings

        await delete_raid_row(current_raid["signup_message_id"])
        return True, warnings


async def create_raid(
    interaction: discord.Interaction,
    raid_name: str,
    date: str,
    time: str,
    *,
    group_name: str | None = None,
    setup_message: str | None = None,
    notify_role: discord.Role | None = None,
    send_announcement: bool = False,
    meeting_server: str | None = None,
):
    if interaction.channel is None or interaction.guild is None:
        await interaction.followup.send("This command needs to be used in a server channel.", ephemeral=True)
        return

    try:
        raid_name = clean_short_text(raid_name, "Raid name", 100)
        group_name = clean_short_text(group_name, "Group name", 50)
        meeting_server = clean_short_text(meeting_server, "Meeting server", 100)
        if raid_name is None:
            raise ValueError("Raid name cannot be blank.")
        scheduled_at = parse_raid_datetime(date, time, DEFAULT_TIMEZONE_NAME)
    except ValueError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return

    raid = {
        "signup_message_id": 0,
        "roster_message_id": 0,
        "channel_id": interaction.channel_id,
        "raid_name": raid_name,
        "date": date,
        "time": time,
        "signups": {},
        "standby": {},
        "scheduled_at": scheduled_at,
        "timezone": DEFAULT_TIMEZONE_NAME,
        "locked": False,
        "group_name": group_name,
        "reminders_sent": {},
        "reminder_offset": choose_reminder_offset(scheduled_at),
        "raid_role_id": None,
        "raid_role_name": DEFAULT_RAID_ROLE_NAME,
        "announcement_message_id": None,
        "setup_message": clean_setup_message(setup_message),
        "notify_role_id": notify_role.id if notify_role else None,
        "notify_role_name": notify_role.name if notify_role else None,
        "meeting_server": meeting_server,
    }

    if interaction.guild is not None:
        existing_role = find_role_by_name(interaction.guild, DEFAULT_RAID_ROLE_NAME)
        if existing_role:
            raid["raid_role_id"] = existing_role.id

    announcement_warning = None
    created_messages = []
    try:
        signup_message = await interaction.channel.send(embed=signup_embed(raid))
        created_messages.append(signup_message)
        raid["signup_message_id"] = signup_message.id

        if send_announcement:
            announcement_role = notify_role or find_role_by_name(interaction.guild, DEFAULT_NOTIFY_ROLE_NAME)
            announcement_text = raid.get("setup_message") or default_setup_message(raid["raid_name"])
            announcement_prefix = ""
            allowed_mentions = discord.AllowedMentions.none()

            if announcement_role is not None:
                raid["notify_role_id"] = announcement_role.id
                raid["notify_role_name"] = announcement_role.name
                announcement_prefix = f"{announcement_role.mention} "
                allowed_mentions = discord.AllowedMentions(
                    everyone=False,
                    users=False,
                    roles=[announcement_role],
                    replied_user=False,
                )
            else:
                announcement_warning = f" I could not find a role named `{DEFAULT_NOTIFY_ROLE_NAME}` to ping."

            announcement_message = await interaction.channel.send(
                f"{announcement_prefix}{announcement_text}\n\nRole signup: {signup_message.jump_url}",
                allowed_mentions=allowed_mentions,
            )
            created_messages.append(announcement_message)
            raid["announcement_message_id"] = announcement_message.id

        roster_message = await interaction.channel.send(embed=roster_embed(raid))
        created_messages.append(roster_message)
        raid["roster_message_id"] = roster_message.id
    except discord.HTTPException as exc:
        await delete_created_messages(created_messages, "raid setup failed")
        logger.error(
            "Could not create raid messages",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        await interaction.followup.send(
            "I could not create the raid posts. Check my View Channel, Send Messages, and Embed Links permissions.",
            ephemeral=True,
        )
        return

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO signups (
                    signup_message_id, roster_message_id, channel_id, raid_name, date, time,
                    signups, standby, scheduled_at, timezone, locked,
                    group_name, reminders_sent, reminder_offset,
                    raid_role_id, raid_role_name, announcement_message_id, setup_message,
                    notify_role_id, notify_role_name, meeting_server
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    raid["signup_message_id"],
                    raid["roster_message_id"],
                    raid["channel_id"],
                    raid["raid_name"],
                    raid["date"],
                    raid["time"],
                    encode_json(raid["signups"]),
                    encode_json(raid["standby"]),
                    raid["scheduled_at"],
                    raid["timezone"],
                    int(raid["locked"]),
                    raid["group_name"],
                    encode_json(raid["reminders_sent"]),
                    raid["reminder_offset"],
                    raid["raid_role_id"],
                    raid["raid_role_name"],
                    raid["announcement_message_id"],
                    raid["setup_message"],
                    raid["notify_role_id"],
                    raid["notify_role_name"],
                    raid["meeting_server"],
                ),
            )
            await db.commit()
    except aiosqlite.Error as exc:
        await delete_created_messages(created_messages, "database failure")
        logger.error(
            "Could not save new raid %s",
            raid["signup_message_id"],
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        await interaction.followup.send("The raid posts could not be saved. Please try again.", ephemeral=True)
        return

    try:
        await signup_message.edit(embed=signup_embed(raid), view=RaidSignupView(raid))
    except discord.HTTPException:
        logger.exception("Could not activate signup buttons for %s", raid["signup_message_id"])
        announcement_warning = (
            f"{announcement_warning or ''} I created the posts but could not activate the signup buttons."
        )

    await interaction.followup.send(
        f"Raid signup created. Signup message ID: `{raid['signup_message_id']}`{announcement_warning or ''}",
        ephemeral=True,
    )


class MountNeedView(ErrorHandledView):
    def __init__(self, signup_message_id: int, user_id: int):
        super().__init__(timeout=300)
        self.signup_message_id = signup_message_id
        self.user_id = user_id
        self.add_mount_button(True)
        self.add_mount_button(False)

    def add_mount_button(self, needs_mount: bool):
        button = discord.ui.Button(
            label="Yes" if needs_mount else "No",
            style=discord.ButtonStyle.success if needs_mount else discord.ButtonStyle.secondary,
            custom_id=f"mount:{self.signup_message_id}:{self.user_id}:{int(needs_mount)}",
        )
        button.callback = self.create_mount_callback(needs_mount)
        self.add_item(button)

    def create_mount_callback(self, needs_mount: bool):
        async def callback(interaction: discord.Interaction):
            await self.set_mount_status(interaction, needs_mount)

        return callback

    async def set_mount_status(self, interaction: discord.Interaction, needs_mount: bool):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This mount picker is not yours.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=False)
        async with get_raid_lock(self.signup_message_id):
            raid = await fetch_signup(self.signup_message_id)
            if not raid:
                await interaction.followup.send("I could not find this raid signup anymore.", ephemeral=True)
                return

            updated = False
            for player in raid["signups"].values():
                if player.get("user_id") == interaction.user.id:
                    player["needs_mount"] = needs_mount
                    updated = True
                    break

            standby_entry = raid["standby"].get(str(interaction.user.id))
            if standby_entry:
                standby_entry["needs_mount"] = needs_mount
                updated = True

            if not updated:
                await interaction.followup.send("You are not on this roster or standby list anymore.", ephemeral=True)
                return

            await save_raid_state(raid)
            await update_raid_messages(interaction.channel, raid)
        result = "Needs Mount" if needs_mount else "Doesn't Need Mount"
        await interaction.edit_original_response(content=f"Mount status saved: **{result}**.", view=None)


def standby_panel_text(raid: dict, user_id: int) -> str:
    entry = raid["standby"].get(str(user_id))
    roles = entry["roles"] if entry else []
    selected = standby_role_text(roles) if roles else "None"
    return (
        f"Standby roles for **{raid_title(raid)}**: **{selected}**\n"
        "Toggle any roles you can cover, then press **Confirm Standby**."
    )


class StandbyRoleView(ErrorHandledView):
    def __init__(self, signup_message_id: int, user_id: int):
        super().__init__(timeout=300)
        self.signup_message_id = signup_message_id
        self.user_id = user_id
        self.add_role_buttons()
        self.add_confirm_button()
        self.add_clear_button()

    def add_role_buttons(self):
        for index, role_name in enumerate(STANDBY_CHOICES):
            button = discord.ui.Button(
                label=role_name,
                style=discord.ButtonStyle.secondary,
                custom_id=f"standby:{self.signup_message_id}:{self.user_id}:{role_name}",
                row=0,
            )
            button.callback = self.create_toggle_callback(role_name)
            self.add_item(button)

    def add_confirm_button(self):
        button = discord.ui.Button(
            label="Confirm Standby",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=f"standby:{self.signup_message_id}:{self.user_id}:confirm",
            row=1,
        )
        button.callback = self.confirm_callback
        self.add_item(button)

    def add_clear_button(self):
        button = discord.ui.Button(
            label="Clear Standby",
            emoji="🧹",
            style=discord.ButtonStyle.danger,
            custom_id=f"standby:{self.signup_message_id}:{self.user_id}:clear",
            row=1,
        )
        button.callback = self.clear_callback
        self.add_item(button)

    def create_toggle_callback(self, role_name: str):
        async def callback(interaction: discord.Interaction):
            await self.toggle_role(interaction, role_name)

        return callback

    async def toggle_role(self, interaction: discord.Interaction, role_name: str):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This standby picker is not yours.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=False)
        async with get_raid_lock(self.signup_message_id):
            raid = await fetch_signup(self.signup_message_id)
            if not raid:
                await interaction.followup.send("I could not find this raid signup anymore.", ephemeral=True)
                return

            if raid["locked"]:
                await interaction.followup.send("This roster is locked.", ephemeral=True)
                return

            if user_is_signed_up(raid, interaction.user.id):
                remove_user_from_standby(raid["standby"], interaction.user.id)
                await save_raid_state(raid)
                await update_raid_messages(interaction.channel, raid)
                await interaction.followup.send(
                    "You already have a main role, so standby was cleared.",
                    ephemeral=True,
                )
                return

            user_key = str(interaction.user.id)
            entry = raid["standby"].setdefault(user_key, player_from_user(interaction.user) | {"roles": []})
            roles = entry["roles"]
            if role_name in roles:
                roles.remove(role_name)
            else:
                roles.append(role_name)

            entry["roles"] = [role for role in STANDBY_CHOICES if role in roles]
            if not entry["roles"]:
                remove_user_from_standby(raid["standby"], interaction.user.id)

            await save_raid_state(raid)
            await update_raid_messages(interaction.channel, raid)
            await interaction.edit_original_response(content=standby_panel_text(raid, interaction.user.id), view=self)

    async def confirm_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This standby picker is not yours.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=False)
        async with get_raid_lock(self.signup_message_id):
            raid = await fetch_signup(self.signup_message_id)
            if not raid:
                await interaction.followup.send("I could not find this raid signup anymore.", ephemeral=True)
                return

            entry = raid["standby"].get(str(interaction.user.id))
            if not entry:
                await interaction.followup.send("Pick at least one standby role before confirming.", ephemeral=True)
                return

        await interaction.edit_original_response(
            content=f"Standby roles confirmed: **{standby_role_text(entry['roles'])}**.",
            view=None,
        )
        if entry.get("needs_mount") is None:
            await interaction.followup.send(
                "Do you still need the mount?",
                view=MountNeedView(self.signup_message_id, interaction.user.id),
                ephemeral=True,
            )

    async def clear_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This standby picker is not yours.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=False)
        async with get_raid_lock(self.signup_message_id):
            raid = await fetch_signup(self.signup_message_id)
            if not raid:
                await interaction.followup.send("I could not find this raid signup anymore.", ephemeral=True)
                return

            remove_user_from_standby(raid["standby"], interaction.user.id)
            await save_raid_state(raid)
            await update_raid_messages(interaction.channel, raid)
            await interaction.edit_original_response(content=standby_panel_text(raid, interaction.user.id), view=self)


class RaidSignupView(ErrorHandledView):
    def __init__(self, raid: dict):
        super().__init__(timeout=None)
        self.signup_message_id = raid["signup_message_id"]
        self.add_role_buttons(raid)
        self.add_standby_button(raid)
        self.add_resign_button(raid)

    def add_role_buttons(self, raid: dict):
        for index, (role_key, role_label, role_emoji, style, _) in enumerate(ROLES):
            button = discord.ui.Button(
                label=role_label,
                emoji=role_emoji,
                style=style,
                custom_id=f"raid_signup:{self.signup_message_id}:role:{role_key}",
                row=index // 2,
                disabled=raid["locked"] or role_key in raid["signups"],
            )
            button.callback = self.create_role_callback(role_key, role_label)
            self.add_item(button)

    def add_standby_button(self, raid: dict):
        button = discord.ui.Button(
            label="Standby",
            emoji="🕯️",
            style=discord.ButtonStyle.secondary,
            custom_id=f"raid_signup:{self.signup_message_id}:standby",
            row=4,
            disabled=raid["locked"],
        )
        button.callback = self.standby_callback
        self.add_item(button)

    def add_resign_button(self, raid: dict):
        button = discord.ui.Button(
            label="Resign",
            emoji="🚪",
            style=discord.ButtonStyle.secondary,
            custom_id=f"raid_signup:{self.signup_message_id}:resign",
            row=4,
            disabled=raid["locked"],
        )
        button.callback = self.resign_callback
        self.add_item(button)

    def create_role_callback(self, role_key: str, role_label: str):
        async def callback(interaction: discord.Interaction):
            await self.handle_role(interaction, role_key, role_label)

        return callback

    async def handle_role(self, interaction: discord.Interaction, role_key: str, role_label: str):
        await interaction.response.defer(ephemeral=True, thinking=False)

        async with get_raid_lock(self.signup_message_id):
            raid = await fetch_signup(self.signup_message_id)
            if not raid:
                await interaction.followup.send("I could not find this raid signup anymore.", ephemeral=True)
                return

            if raid["locked"]:
                await interaction.followup.send("This roster is locked.", ephemeral=True)
                return

            if role_key in raid["signups"]:
                await interaction.followup.send(
                    f"**{role_label}** is already taken. Use the **Standby** button if you can flex.",
                    ephemeral=True,
                )
                return

            user_id = interaction.user.id
            previous_role_key = None
            needs_mount = None
            for existing_role_key, player in list(raid["signups"].items()):
                if player.get("user_id") == user_id:
                    previous_role_key = existing_role_key
                    needs_mount = player.get("needs_mount")
                    del raid["signups"][existing_role_key]
                    break

            standby_entry = raid["standby"].get(str(user_id))
            if needs_mount is None and standby_entry:
                needs_mount = standby_entry.get("needs_mount")

            remove_user_from_standby(raid["standby"], user_id)
            raid["signups"][role_key] = player_from_user(interaction.user) | {"needs_mount": needs_mount}
            await save_raid_state(raid)

            role_warning = None
            if isinstance(interaction.user, discord.Member):
                role_warning = await add_raid_role(interaction.user, raid)

            await self.refresh_messages(interaction, raid)

            if previous_role_key:
                previous_role_label = ROLE_LABELS.get(previous_role_key, previous_role_key)
                message = f"You moved from **{previous_role_label}** to **{role_label}**."
            else:
                message = f"You signed up as **{role_label}**."

            if role_warning:
                message += f"\n{role_warning}"

        await interaction.followup.send(message, ephemeral=True)
        if needs_mount is None:
            await interaction.followup.send(
                "Do you still need the mount?",
                view=MountNeedView(self.signup_message_id, interaction.user.id),
                ephemeral=True,
            )

    async def standby_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        raid = await fetch_signup(self.signup_message_id)
        if not raid:
            await interaction.followup.send("I could not find this raid signup anymore.", ephemeral=True)
            return

        if raid["locked"]:
            await interaction.followup.send("This roster is locked.", ephemeral=True)
            return

        if user_is_signed_up(raid, interaction.user.id):
            await interaction.followup.send(
                "You already have a main role. Resign first if you want to move to standby.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            standby_panel_text(raid, interaction.user.id),
            view=StandbyRoleView(self.signup_message_id, interaction.user.id),
            ephemeral=True,
        )

    async def resign_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)

        async with get_raid_lock(self.signup_message_id):
            raid = await fetch_signup(self.signup_message_id)
            if not raid:
                await interaction.followup.send("I could not find this raid signup anymore.", ephemeral=True)
                return

            if raid["locked"]:
                await interaction.followup.send("This roster is locked.", ephemeral=True)
                return

            user_id = interaction.user.id
            removed_role_label = None
            role_warning = None

            for role_key, player in list(raid["signups"].items()):
                if player.get("user_id") == user_id:
                    del raid["signups"][role_key]
                    removed_role_label = ROLE_LABELS.get(role_key, role_key)
                    break

            removed_from_standby = remove_user_from_standby(raid["standby"], user_id)

            if removed_role_label or removed_from_standby:
                await save_raid_state(raid)

                if removed_role_label and isinstance(interaction.user, discord.Member):
                    role_warning = await remove_raid_role(interaction.user, raid)

                await self.refresh_messages(interaction, raid)

                if removed_role_label and removed_from_standby:
                    message = "You have resigned from the roster entirely."
                elif removed_role_label:
                    message = f"You've resigned from the roster as **{removed_role_label}**."
                else:
                    message = "You have resigned from the standby list."

                if role_warning:
                    message += f"\n{role_warning}"

                await interaction.followup.send(message, ephemeral=True)
                return

        await interaction.followup.send("You're not registered for any role or standby.", ephemeral=True)

    async def refresh_messages(self, interaction: discord.Interaction, raid: dict):
        channel = interaction.channel
        if channel is None and raid.get("channel_id"):
            channel = bot.get_channel(raid["channel_id"])

        await update_raid_messages(channel, raid)


@tree.command(name="raidsetup", description="Create a raid signup with an announcement message and notification ping")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(
    raid_name="Name of the raid",
    date="Raid date, such as 5/25, May 25, or 2026-05-25",
    time="Raid time, such as 20:00, 8:00 PM, 8PM, or 8 PM",
    meeting_server="Optional server/world where everyone should gather",
    message="Optional message sent with the notification ping",
    notify_role=f"Optional role to ping. Defaults to {DEFAULT_NOTIFY_ROLE_NAME}.",
    group="Optional group or team name, such as Group A",
)
async def raidsetup(
    interaction: discord.Interaction,
    raid_name: str,
    date: str,
    time: str,
    meeting_server: str | None = None,
    message: str | None = None,
    notify_role: discord.Role | None = None,
    group: str | None = None,
):
    await interaction.response.defer(ephemeral=True, thinking=True)
    allowed, error = require_roster_manager(interaction)
    if not allowed:
        await interaction.followup.send(error, ephemeral=True)
        return

    setup_message = clean_setup_message(message) or default_setup_message(raid_name)
    await create_raid(
        interaction,
        raid_name,
        date,
        time,
        group_name=group,
        setup_message=setup_message,
        notify_role=notify_role,
        send_announcement=True,
        meeting_server=meeting_server,
    )


@tree.command(name="raidpreset", description="Create a raid signup from a preset raid name")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(
    preset="Preset raid",
    date="Raid date, such as 5/25, May 25, or 2026-05-25",
    time="Raid time, such as 20:00, 8:00 PM, 8PM, or 8 PM",
    group="Optional group or team name",
)
@app_commands.choices(preset=PRESET_CHOICES)
async def raidpreset(
    interaction: discord.Interaction,
    preset: app_commands.Choice[str],
    date: str,
    time: str,
    group: str | None = None,
):
    await interaction.response.defer(ephemeral=True, thinking=True)
    allowed, error = require_roster_manager(interaction)
    if not allowed:
        await interaction.followup.send(error, ephemeral=True)
        return

    await create_raid(interaction, preset.value, date, time, group_name=group)


@tree.command(name="lockroster", description="Lock a roster so players cannot change roles")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(signup_message_id="Optional signup message ID. Leave blank for newest signup in this channel.")
async def lockroster(interaction: discord.Interaction, signup_message_id: str | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    allowed, error = require_roster_manager(interaction)
    if not allowed:
        await interaction.followup.send(error, ephemeral=True)
        return

    raid, error = await resolve_raid_for_command(interaction, signup_message_id)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    async with get_raid_lock(raid["signup_message_id"]):
        raid = await fetch_signup(raid["signup_message_id"])
        if not raid:
            await interaction.followup.send("That roster was already deleted.", ephemeral=True)
            return
        raid["locked"] = True
        await save_raid_state(raid)
        await update_raid_messages(interaction.channel, raid)
    await interaction.followup.send(f"Locked **{raid_title(raid)}**.", ephemeral=True)


@tree.command(name="unlockroster", description="Unlock a roster so players can change roles again")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(signup_message_id="Optional signup message ID. Leave blank for newest signup in this channel.")
async def unlockroster(interaction: discord.Interaction, signup_message_id: str | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    allowed, error = require_roster_manager(interaction)
    if not allowed:
        await interaction.followup.send(error, ephemeral=True)
        return

    raid, error = await resolve_raid_for_command(interaction, signup_message_id)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    async with get_raid_lock(raid["signup_message_id"]):
        raid = await fetch_signup(raid["signup_message_id"])
        if not raid:
            await interaction.followup.send("That roster was already deleted.", ephemeral=True)
            return
        raid["locked"] = False
        await save_raid_state(raid)
        await update_raid_messages(interaction.channel, raid)
    await interaction.followup.send(f"Unlocked **{raid_title(raid)}**.", ephemeral=True)


@tree.command(name="missingroles", description="Show open roles for a roster")
@app_commands.describe(signup_message_id="Optional signup message ID. Leave blank for newest signup in this channel.")
async def missingroles(interaction: discord.Interaction, signup_message_id: str | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    raid, error = await resolve_raid_for_command(interaction, signup_message_id)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    missing = missing_role_labels(raid["signups"])
    await interaction.followup.send(
        f"Missing roles for **{raid_title(raid)}**: {', '.join(missing) if missing else 'none.'}",
        ephemeral=True,
    )


@tree.command(name="remindraid", description="Send a manual reminder to signed players")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(signup_message_id="Optional signup message ID. Leave blank for newest signup in this channel.")
async def remindraid(interaction: discord.Interaction, signup_message_id: str | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    allowed, error = require_roster_manager(interaction)
    if not allowed:
        await interaction.followup.send(error, ephemeral=True)
        return

    raid, error = await resolve_raid_for_command(interaction, signup_message_id)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    ping_text = reminder_ping_text(interaction.channel, raid)
    await interaction.channel.send(
        f"Raid reminder for **{raid_title(raid)}**.\n"
        f"{schedule_text(raid)}\n"
        f"Ping: {ping_text}",
        allowed_mentions=allowed_mentions_for_raid(interaction.channel, raid),
    )
    await interaction.followup.send("Reminder sent.", ephemeral=True)


@tree.command(name="roster", description="Export a copy-paste roster")
@app_commands.describe(signup_message_id="Optional signup message ID. Leave blank for newest signup in this channel.")
async def roster(interaction: discord.Interaction, signup_message_id: str | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    raid, error = await resolve_raid_for_command(interaction, signup_message_id)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    for chunk in chunk_text_lines(roster_export_text(raid)):
        await interaction.followup.send(f"```text\n{chunk}\n```", ephemeral=True)


@tree.command(name="raidrole", description="Set the Discord role assigned to signed players and pinged by reminders")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(
    role="Discord role to assign to signed players and ping for reminders",
    signup_message_id="Optional signup message ID. Leave blank for newest signup in this channel.",
)
async def raidrole(
    interaction: discord.Interaction,
    role: discord.Role,
    signup_message_id: str | None = None,
):
    await interaction.response.defer(ephemeral=True, thinking=True)
    allowed, error = require_roster_manager(interaction)
    if not allowed:
        await interaction.followup.send(error, ephemeral=True)
        return

    raid, error = await resolve_raid_for_command(interaction, signup_message_id)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    if interaction.guild is None:
        await interaction.followup.send("This command needs to be used in a server.", ephemeral=True)
        return

    bot_member = interaction.guild.me
    if role.is_default() or role.managed or bot_member is None or role >= bot_member.top_role:
        await interaction.followup.send(
            "I cannot manage that role. Choose a regular role below my bot role in the server role list.",
            ephemeral=True,
        )
        return

    async with get_raid_lock(raid["signup_message_id"]):
        raid = await fetch_signup(raid["signup_message_id"])
        if not raid:
            await interaction.followup.send("That roster was already deleted.", ephemeral=True)
            return

        warnings = []
        old_role = raid_role_for_guild(interaction.guild, raid)
        old_role_raid = raid.copy()

        raid["raid_role_id"] = role.id
        raid["raid_role_name"] = role.name
        await save_raid_state(raid)

        if old_role and old_role.id != role.id:
            for player in raid["signups"].values():
                member = await fetch_guild_member(interaction.guild, player.get("user_id"))
                if member and old_role in member.roles:
                    warning = await remove_raid_role(member, old_role_raid)
                    if warning and warning not in warnings:
                        warnings.append(warning)

        for player in raid["signups"].values():
            member = await fetch_guild_member(interaction.guild, player.get("user_id"))
            if member:
                warning = await add_raid_role(member, raid)
                if warning and warning not in warnings:
                    warnings.append(warning)

        await update_raid_messages(interaction.channel, raid)

    message = f"Raid role set to {role.mention} for **{raid_title(raid)}**."
    if warnings:
        message += "\n" + "\n".join(warnings)

    await interaction.followup.send(message, ephemeral=True)


@tree.command(name="bumproster", description="Move the roster post to the bottom of the channel")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(signup_message_id="Optional signup message ID. Leave blank for newest signup in this channel.")
async def bumproster(interaction: discord.Interaction, signup_message_id: str | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    allowed, error = require_roster_manager(interaction)
    if not allowed:
        await interaction.followup.send(error, ephemeral=True)
        return

    raid, error = await resolve_raid_for_command(interaction, signup_message_id)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    warning = None
    async with get_raid_lock(raid["signup_message_id"]):
        raid = await fetch_signup(raid["signup_message_id"])
        if not raid:
            await interaction.followup.send("That roster was already deleted.", ephemeral=True)
            return

        old_roster_message_id = raid.get("roster_message_id")
        new_roster_message = await interaction.channel.send(embed=roster_embed(raid))
        raid["roster_message_id"] = new_roster_message.id
        try:
            await save_raid_state(raid)
        except aiosqlite.Error:
            try:
                await new_roster_message.delete()
            except discord.HTTPException:
                logger.exception("Could not remove unsaved bumped roster message %s", new_roster_message.id)
            raise

        if old_roster_message_id:
            try:
                old_roster_message = await interaction.channel.fetch_message(old_roster_message_id)
                await old_roster_message.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                warning = " I could not delete the old roster post, so you may see a duplicate."

    await interaction.followup.send(
        f"Bumped the roster for **{raid_title(raid)}**.{warning or ''}",
        ephemeral=True,
    )


@tree.command(name="clearroster", description="Clear a raid roster and remove assigned raid roles")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(signup_message_id="Optional signup message ID. Leave blank for newest signup in this channel.")
async def clearroster(interaction: discord.Interaction, signup_message_id: str | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    allowed, error = require_roster_manager(interaction)
    if not allowed:
        await interaction.followup.send(error, ephemeral=True)
        return

    raid, error = await resolve_raid_for_command(interaction, signup_message_id)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    async with get_raid_lock(raid["signup_message_id"]):
        raid = await fetch_signup(raid["signup_message_id"])
        if not raid:
            await interaction.followup.send("That roster was already deleted.", ephemeral=True)
            return

        signed_players = list(raid["signups"].values())
        reset_roster_state(raid)
        await save_raid_state(raid)

        warnings = []
        for player in signed_players:
            warning = await remove_raid_role_from_player(interaction.guild, raid, player)
            if warning and warning not in warnings:
                warnings.append(warning)

        await update_raid_messages(interaction.channel, raid)

    message = f"Cleared **{raid_title(raid)}**."
    if warnings:
        message += "\n" + "\n".join(warnings)
    await interaction.followup.send(message, ephemeral=True)


@tree.command(name="deleteroster", description="Clear a roster, remove raid roles, and delete bot posts")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(signup_message_id="Optional signup message ID. Leave blank for newest signup in this channel.")
async def deleteroster(interaction: discord.Interaction, signup_message_id: str | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    allowed, error = require_roster_manager(interaction)
    if not allowed:
        await interaction.followup.send(error, ephemeral=True)
        return

    raid, error = await resolve_raid_for_command(interaction, signup_message_id)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    deleted, warnings = await delete_raid_for_command(interaction, raid)
    message = (
        f"Deleted roster for **{raid_title(raid)}**."
        if deleted
        else f"Could not fully delete the roster for **{raid_title(raid)}**."
    )
    if warnings:
        message += "\n" + "\n".join(warnings)
    await interaction.followup.send(message, ephemeral=True)


@tree.command(name="deleteraid", description="Delete a raid signup, roster post, and database entry")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(signup_message_id="Optional signup message ID. Leave blank for newest signup in this channel.")
async def deleteraid(interaction: discord.Interaction, signup_message_id: str | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    allowed, error = require_roster_manager(interaction)
    if not allowed:
        await interaction.followup.send(error, ephemeral=True)
        return

    raid, error = await resolve_raid_for_command(interaction, signup_message_id)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    deleted, warnings = await delete_raid_for_command(interaction, raid)
    message = f"Deleted **{raid_title(raid)}**." if deleted else f"Could not fully delete **{raid_title(raid)}**."
    if warnings:
        message += "\n" + "\n".join(warnings)
    await interaction.followup.send(message, ephemeral=True)


@tree.command(name="help", description="Show the raid signup bot commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="RosterBot Coordinator Help",
        description=(
            "Use this bot to create FFXIV raid signups, assign one player per role, "
            "track standby players, send reminders, and keep the roster readable."
        ),
        color=0x5865F2,
    )
    embed.add_field(
        name="/raidsetup",
        value=(
            "Creates a raid signup and sends a separate announcement message "
            f"that pings `{DEFAULT_NOTIFY_ROLE_NAME}` by default. This ping role is only notified; players still "
            f"only receive `{DEFAULT_RAID_ROLE_NAME}` when they pick a roster role. `meeting_server` is used for "
            "the 5-minute raid-start ping. Requires Manage Messages."
        ),
        inline=False,
    )
    embed.add_field(
        name="/raidpreset",
        value=(
            "Creates the same signup using a preset raid name, such as Everkeep EX, M4S, or FRU. "
            "Use this when you do not want to type the full raid name. Requires Manage Messages."
        ),
        inline=False,
    )
    embed.add_field(
        name="Buttons",
        value=(
            "Role buttons claim open roles. Filled roles are locked. `Standby` lets unsigned players "
            "mark Tank, DPS, Healer, or all three as Any. `Resign` removes you from both the main roster "
            "and standby, then reopens your main-party spot. "
            "Main roster players receive the configured raid role."
        ),
        inline=False,
    )
    embed.add_field(
        name="Standby List",
        value=(
            "Click `Standby`, toggle every role type you can cover, then press `Confirm Standby`. "
            "The roster shows entries like `@User - Tank`, `@User - DPS/Healer`, or `@User - Any`."
        ),
        inline=False,
    )
    embed.add_field(
        name="/lockroster and /unlockroster",
        value=(
            "Locks or unlocks a roster. Locked rosters stop role changes and resigns. "
            "Requires Manage Messages."
        ),
        inline=False,
    )
    embed.add_field(
        name="/clearroster, /deleteroster, and /deleteraid",
        value=(
            "`/clearroster` removes assigned raid roles and resets the roster while keeping the bot posts. "
            "`/deleteroster` removes assigned raid roles, deletes the signup and roster posts, "
            "and removes the saved roster. "
            "`/deleteraid` also deletes both bot posts and the saved roster. Requires Manage Messages."
        ),
        inline=False,
    )
    embed.add_field(
        name="/bumproster",
        value=(
            "Deletes the old roster post and reposts the same current roster at the bottom of the channel. "
            "The saved roster data stays intact. Requires Manage Messages."
        ),
        inline=False,
    )
    embed.add_field(
        name="/missingroles and /roster",
        value=(
            "`/missingroles` shows open slots. `/roster` gives a copy-paste roster export. "
            "Both default to the newest signup in the channel."
        ),
        inline=False,
    )
    embed.add_field(
        name="/remindraid",
        value=(
            "Sends a manual reminder message that pings the configured raid role. "
            "Requires Manage Messages."
        ),
        inline=False,
    )
    embed.add_field(
        name="/raidrole",
        value=(
            "Sets the Discord role assigned to signed players and pinged by reminders for an existing roster. "
            "The bot needs Manage Roles, and its bot role must be above the raid role. Requires Manage Messages."
        ),
        inline=False,
    )
    embed.add_field(
        name="Reminders",
        value=(
            "The bot sends one automatic reminder only. It chooses the longest available reminder window: "
            "24 hours, otherwise 12 hours, otherwise 8 hours, otherwise 6 hours before raid time. "
            "The reminder pings the configured raid role. It also sends a separate 5-minute ping with the "
            "meeting server and a random cheerful message. Two hours after raid start, the bot removes "
            "the raid role, deletes the raid posts, and removes the saved roster. The bot must be running "
            "for reminders and cleanup."
        ),
        inline=False,
    )
    embed.add_field(
        name="Targeting Older Rosters",
        value=(
            "Most management commands accept `signup_message_id`. Leave it blank to use the newest signup "
            "in the channel, or paste the ID from the signup embed footer to target a specific roster."
        ),
        inline=False,
    )
    embed.set_footer(text="This help message is only visible to you.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def send_due_reminders():
    now = int(datetime.now(timezone.utc).timestamp())
    raid_snapshots = await fetch_scheduled_raids()

    for snapshot in raid_snapshots:
        signup_message_id = snapshot["signup_message_id"]
        async with get_raid_lock(signup_message_id):
            raid = await fetch_signup(signup_message_id)
            if not raid:
                continue

            scheduled_at = raid.get("scheduled_at")
            if not scheduled_at:
                continue

            channel = await fetch_raid_channel(raid)

            if now >= scheduled_at + (2 * 60 * 60):
                if channel is None:
                    logger.warning("Post-raid cleanup cannot access channel for %s", signup_message_id)
                    continue

                guild = getattr(channel, "guild", None)
                role_warnings = await remove_all_raid_roles(guild, raid)
                for warning in role_warnings:
                    logger.warning("Post-raid cleanup for %s: %s", signup_message_id, warning)

                if await delete_raid_messages(channel, raid):
                    await delete_raid_row(signup_message_id)
                    logger.info("Completed post-raid cleanup for %s", signup_message_id)
                else:
                    logger.warning("Post-raid cleanup could not delete all messages for %s", signup_message_id)
                continue

            if now > scheduled_at:
                continue

            reminder_offset = raid.get("reminder_offset")
            if reminder_offset is None:
                reminder_offset = choose_reminder_offset(scheduled_at)
                raid["reminder_offset"] = reminder_offset
                await save_reminder_state(raid)

            five_minute_due = (
                not raid["reminders_sent"].get("five_minute")
                and scheduled_at - FIVE_MINUTE_REMINDER_SECONDS <= now <= scheduled_at
            )
            if five_minute_due:
                if channel is None:
                    logger.warning("Five-minute reminder cannot access channel for %s", signup_message_id)
                    continue

                try:
                    await channel.send(
                        five_minute_reminder_text(channel, raid),
                        allowed_mentions=allowed_mentions_for_raid(channel, raid),
                    )
                except discord.HTTPException:
                    logger.exception("Could not send five-minute reminder for %s", signup_message_id)
                    continue

                raid["reminders_sent"]["five_minute"] = True
                await save_reminder_state(raid)
                continue

            if not reminder_offset or raid["reminders_sent"].get("automatic"):
                continue

            if now < scheduled_at - reminder_offset:
                continue

            if channel is None:
                logger.warning("Automatic reminder cannot access channel for %s", signup_message_id)
                continue

            try:
                ping_text = reminder_ping_text(channel, raid)
                await channel.send(
                    f"Reminder: **{raid_title(raid)}** starts in {reminder_label(reminder_offset)}.\n"
                    f"{schedule_text(raid)}\n"
                    f"Ping: {ping_text}",
                    allowed_mentions=allowed_mentions_for_raid(channel, raid),
                )
            except discord.HTTPException:
                logger.exception("Could not send automatic reminder for %s", signup_message_id)
                continue

            raid["reminders_sent"]["automatic"] = True
            await save_reminder_state(raid)


async def reminder_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await send_due_reminders()
        except Exception:
            logger.exception("Reminder check failed")
        await asyncio.sleep(60)


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = error.original if isinstance(error, app_commands.CommandInvokeError) else error
    logger.error(
        "Application command failed: %s",
        interaction.command.name if interaction.command else "unknown",
        exc_info=(type(original), original, original.__traceback__),
    )

    if isinstance(original, discord.Forbidden):
        message = "I am missing a Discord permission needed for that command. Please check my channel permissions."
    else:
        message = "Something went wrong while running that command. Please try again."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        logger.exception("Could not report an application command error")


@bot.event
async def on_ready():
    global views_loaded, reminder_task

    await init_db()

    if not views_loaded:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                """
                SELECT signup_message_id, roster_message_id, channel_id, raid_name, date, time,
                       signups, waitlist, standby, confirmations, scheduled_at, timezone, locked,
                       role_restrictions, group_name, reminders_sent, reminder_offset,
                       raid_role_id, raid_role_name, announcement_message_id, setup_message,
                       notify_role_id, notify_role_name, meeting_server
                FROM signups
                """
            ) as cursor:
                rows = await cursor.fetchall()

        for row in rows:
            raid = raid_from_row(row)
            bot.add_view(RaidSignupView(raid), message_id=raid["signup_message_id"])

        await tree.sync()
        views_loaded = True

    if reminder_task is None or reminder_task.done():
        reminder_task = asyncio.create_task(reminder_loop())

    logger.info("Bot ready! Logged in as %s", bot.user)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set.")
    bot.run(token)
