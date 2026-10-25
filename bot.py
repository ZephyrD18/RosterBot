import ast
import asyncio
import json
import logging
import os
import random
from contextlib import suppress
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from weakref import WeakValueDictionary

import aiosqlite
import discord
from discord import app_commands


# --- Configuration and lifecycle ---

class RosterClient(discord.Client):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.reminder_task: asyncio.Task | None = None

    async def setup_hook(self):
        await init_db()
        for raid in await fetch_all_raids():
            self.add_view(RaidSignupView(raid), message_id=raid["signup_message_id"])
        await tree.sync()
        self.reminder_task = asyncio.create_task(reminder_loop(), name="raid-reminders")

    async def close(self):
        try:
            if self.reminder_task is not None:
                self.reminder_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.reminder_task
        finally:
            self.reminder_task = None
            await super().close()


intents = discord.Intents(guilds=True)
bot = RosterClient(intents=intents, allowed_mentions=discord.AllowedMentions.none())
tree = app_commands.CommandTree(bot)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("rosterbot")

DB_PATH = str(Path(os.getenv("ROSTERBOT_DB_PATH", "raids.db")).expanduser())
raid_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
DEFAULT_RAID_ROLE_NAME = "FFXIVActiveRoster"
DEFAULT_NOTIFY_ROLE_NAME = "FFXIV Raid"
DEFAULT_TIMEZONE_NAME = "PT"
role_locks: WeakValueDictionary[tuple[int, int], asyncio.Lock] = WeakValueDictionary()
UNSAFE_RAID_ROLE_PERMISSIONS = discord.Permissions(
    administrator=True,
    kick_members=True,
    ban_members=True,
    manage_channels=True,
    manage_guild=True,
    view_audit_log=True,
    manage_messages=True,
    mention_everyone=True,
    view_guild_insights=True,
    mute_members=True,
    deafen_members=True,
    move_members=True,
    manage_nicknames=True,
    manage_roles=True,
    manage_webhooks=True,
    manage_expressions=True,
    manage_events=True,
    manage_threads=True,
    moderate_members=True,
    view_creator_monetization_analytics=True,
    pin_messages=True,
    bypass_slowmode=True,
)

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
    ("MT", "OT"),
    ("H1", "H2"),
    ("M1", "M2"),
    ("R1", "R2"),
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


# --- Saved data and text helpers ---

def encode_json(value) -> str:
    return json.dumps(value, separators=(",", ":"))


def decode_json(raw: str | None, fallback):
    if not raw:
        return fallback

    try:
        value = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        try:
            value = ast.literal_eval(raw)
        except (SyntaxError, ValueError, TypeError, RecursionError):
            return fallback

    return value if isinstance(value, type(fallback)) else fallback


def normalize_player(player: dict) -> dict:
    user_id = player.get("user_id")
    if isinstance(user_id, str) and len(user_id) <= 20 and user_id.isascii() and user_id.isdecimal():
        user_id = int(user_id)
    if type(user_id) is not int or not 0 < user_id < 2**64:
        user_id = None

    display_name = player.get("display_name")
    if not isinstance(display_name, str) or not display_name:
        display_name = "Unknown"
    mention = player.get("mention")
    if user_id:
        mention = f"<@{user_id}>"
    elif not isinstance(mention, str) or not mention:
        mention = display_name
    needs_mount = player.get("needs_mount")
    return {
        "user_id": user_id,
        "display_name": display_name,
        "mention": mention,
        "needs_mount": needs_mount if type(needs_mount) is bool else None,
    }


def decode_signups(raw: str | None) -> dict:
    value = decode_json(raw, {})
    normalized = {}
    for role_key, player in value.items():
        if role_key not in ROLE_LABELS:
            continue
        if isinstance(player, dict):
            normalized[role_key] = normalize_player(player)
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

        saved_roles = entry.get("roles")
        if not isinstance(saved_roles, (list, tuple, set)):
            continue
        roles = [role for role in STANDBY_CHOICES if role in saved_roles]
        if not roles:
            continue

        player = normalize_player({**entry, "user_id": user_id})
        if player["user_id"] is not None:
            standby[str(player["user_id"])] = {**player, "roles": roles}
    return standby


def decode_reminders(raw: str | None) -> dict:
    value = decode_json(raw, {})
    return {str(key): sent is True for key, sent in value.items()}


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
        return empty_text[:1024]

    visible_lines = []
    visible_length = 0
    for line in lines:
        remaining = len(lines) - len(visible_lines) - 1
        suffix = f"\n... and {remaining} more." if remaining else ""
        added_length = len(line) + bool(visible_lines)
        if visible_length + added_length + len(suffix) > 1024:
            break
        visible_lines.append(line)
        visible_length += added_length

    remaining = len(lines) - len(visible_lines)
    suffix = f"... and {remaining} more." if remaining else ""
    if visible_lines and suffix:
        suffix = "\n" + suffix
    return "\n".join(visible_lines) + suffix


def chunk_text_lines(text: str, max_length: int = 1900) -> list[str]:
    if max_length < 1:
        raise ValueError("Chunk length must be positive.")
    chunks = []
    current_lines = []
    current_length = 0
    for line in text.splitlines():
        if len(line) > max_length:
            if current_lines:
                chunks.append("\n".join(current_lines))
                current_lines = []
                current_length = 0
            parts = [line[index:index + max_length] for index in range(0, len(line), max_length)]
            chunks.extend(parts[:-1])
            line = parts[-1]
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


# --- Discord roles and permissions ---

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
    lock = raid_locks.get(signup_message_id)
    if lock is None:
        lock = asyncio.Lock()
        raid_locks[signup_message_id] = lock
    return lock


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

    warning = raid_role_safety_error(role)
    if warning:
        return warning

    async with get_role_lock(member.id, role.id):
        try:
            # Atomic role endpoints are idempotent; cached member roles can be stale.
            await member.add_roles(role, reason=f"Signed up for {raid_title(raid)}")
        except discord.Forbidden:
            return "I could not assign the raid role. Make sure I have Manage Roles and my bot role is above that role."
        except discord.HTTPException:
            return "Discord could not assign the raid role right now. Your signup was saved."

    return None


async def remove_raid_role(member: discord.Member, raid: dict) -> str | None:
    role = raid_role_for_guild(member.guild, raid)
    if role is None:
        return None

    async with get_role_lock(member.id, role.id):
        for other_raid in await fetch_raids_for_role(role.id, role.name):
            if other_raid["signup_message_id"] == raid["signup_message_id"]:
                continue
            if not user_is_signed_up(other_raid, member.id):
                continue
            if other_raid.get("raid_role_id") == role.id:
                return None
            channel = await fetch_raid_channel(other_raid)
            if channel is None:
                return "I kept the raid role because I could not verify another saved roster using that role."
            if getattr(getattr(channel, "guild", None), "id", None) == member.guild.id:
                other_role = raid_role_for_guild(member.guild, other_raid)
                if other_role is not None and other_role.id == role.id:
                    return None

        try:
            await member.remove_roles(role, reason=f"Left {raid_title(raid)}")
        except discord.Forbidden:
            return "I could not remove the raid role. Make sure I have Manage Roles and my bot role is above that role."
        except discord.HTTPException:
            return "Discord could not remove the raid role right now. A server manager may need to remove it manually."

    return None


def get_role_lock(user_id: int, role_id: int) -> asyncio.Lock:
    key = (user_id, role_id)
    lock = role_locks.get(key)
    if lock is None:
        lock = role_locks[key] = asyncio.Lock()
    return lock


def raid_role_safety_error(role: discord.Role) -> str | None:
    if role.is_default() or role.managed:
        return "Choose a regular server role for raid signups."
    if role.permissions.value & UNSAFE_RAID_ROLE_PERMISSIONS.value:
        return "Raid signup roles cannot grant moderation, administration, or server management permissions."
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

    return member


async def remove_raid_role_from_player(
    guild: discord.Guild | None,
    raid: dict,
    player: dict,
) -> str | None:
    try:
        member = await fetch_guild_member(guild, player.get("user_id"))
    except discord.HTTPException:
        logger.exception("Could not fetch guild member %s for raid role removal", player.get("user_id"))
        return "I could not check a saved player's server membership, so their raid role may still need removal."
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
        meeting_text = f"We are gathering in **{raid['meeting_server']}**!"
    else:
        meeting_text = "Join the VC to figure out where the raid group is meeting!"
    return (
        f"{ping_text}, **{raid_title(raid)}** starts <t:{raid['scheduled_at']}:R>! "
        f"{meeting_text} {cheerful_message}"
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

    permissions = interaction.permissions
    if permissions.manage_messages or permissions.administrator:
        return True, None

    return False, "You need Manage Messages permission to manage raid rosters."


# --- Database ---

RAID_COLUMNS = """
    signup_message_id, roster_message_id, channel_id, raid_name, date, time,
    signups, standby, scheduled_at, timezone, locked, group_name,
    reminders_sent, reminder_offset, raid_role_id, raid_role_name,
    announcement_message_id, setup_message, notify_role_id, notify_role_name,
    meeting_server, cleanup_pending
"""


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
                standby TEXT NOT NULL DEFAULT '{}',
                scheduled_at INTEGER,
                timezone TEXT,
                locked INTEGER NOT NULL DEFAULT 0,
                group_name TEXT,
                reminders_sent TEXT NOT NULL DEFAULT '{}',
                reminder_offset INTEGER,
                raid_role_id INTEGER,
                raid_role_name TEXT,
                announcement_message_id INTEGER,
                setup_message TEXT,
                notify_role_id INTEGER,
                notify_role_name TEXT,
                meeting_server TEXT,
                cleanup_pending INTEGER NOT NULL DEFAULT 0
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
            ("standby", "TEXT NOT NULL DEFAULT '{}'"),
            ("locked", "INTEGER NOT NULL DEFAULT 0"),
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
            ("cleanup_pending", "INTEGER NOT NULL DEFAULT 0"),
        )
        for column_name, column_type in migrations:
            if column_name not in columns:
                await db.execute(f"ALTER TABLE signups ADD COLUMN {column_name} {column_type}")

        await db.execute(
            "CREATE INDEX IF NOT EXISTS signups_channel_latest ON signups(channel_id, signup_message_id DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS signups_scheduled ON signups(scheduled_at) WHERE scheduled_at IS NOT NULL"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS signups_cleanup ON signups(cleanup_pending) WHERE cleanup_pending = 1"
        )
        await db.commit()


def raid_from_row(row) -> dict | None:
    if not row:
        return None

    raid = dict(row)
    raid["signups"] = decode_signups(raid["signups"])
    raid["standby"] = decode_standby(raid["standby"])
    raid["reminders_sent"] = decode_reminders(raid["reminders_sent"])
    raid["locked"] = bool(raid["locked"])
    raid["cleanup_pending"] = bool(raid["cleanup_pending"])
    return raid


async def fetch_signup(signup_message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {RAID_COLUMNS} FROM signups WHERE signup_message_id = ?",
            (signup_message_id,),
        ) as cursor:
            return raid_from_row(await cursor.fetchone())


async def fetch_latest_signup_for_channel(channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""
            SELECT {RAID_COLUMNS}
            FROM signups
            WHERE channel_id = ? AND cleanup_pending = 0
            ORDER BY signup_message_id DESC
            LIMIT 1
            """,
            (channel_id,),
        ) as cursor:
            return raid_from_row(await cursor.fetchone())


async def fetch_all_raids():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {RAID_COLUMNS} FROM signups WHERE cleanup_pending = 0",
        ) as cursor:
            return [raid_from_row(row) for row in await cursor.fetchall()]


async def fetch_raids_for_role(role_id: int, role_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""
            SELECT {RAID_COLUMNS} FROM signups
            WHERE cleanup_pending = 0 AND (
                raid_role_id = ? OR raid_role_name = ?
                OR ((raid_role_name IS NULL OR raid_role_name = '') AND ? = ?)
            )
            """,
            (role_id, role_name, role_name, DEFAULT_RAID_ROLE_NAME),
        ) as cursor:
            return [raid_from_row(row) for row in await cursor.fetchall()]


async def fetch_scheduled_raids(now: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {RAID_COLUMNS} FROM signups WHERE cleanup_pending = 1 OR scheduled_at <= ?",
            (now + REMINDER_WINDOWS[0][0] + REMINDER_SELECTION_GRACE_SECONDS,),
        ) as cursor:
            return [raid_from_row(row) for row in await cursor.fetchall()]


async def save_raid_state(raid: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE signups
            SET roster_message_id = ?, signups = ?, standby = ?, locked = ?,
                group_name = ?, reminders_sent = ?, reminder_offset = ?,
                raid_role_id = ?, raid_role_name = ?, announcement_message_id = ?,
                setup_message = ?, notify_role_id = ?, notify_role_name = ?, meeting_server = ?
            WHERE signup_message_id = ? AND cleanup_pending = 0
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
        if cursor.rowcount == 0:
            raise ValueError("That roster was deleted or is awaiting role cleanup.")
        await db.commit()


async def insert_raid(raid: dict) -> None:
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
    stop_raid_views(signup_message_id)


def stop_raid_views(signup_message_id: int):
    for view in bot.persistent_views:
        if isinstance(view, RaidSignupView) and view.signup_message_id == signup_message_id:
            view.stop()


async def mark_raid_cleanup_pending(raid: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE signups SET cleanup_pending = 1, locked = 1 WHERE signup_message_id = ?",
            (raid["signup_message_id"],),
        )
        await db.commit()
    raid["cleanup_pending"] = True
    raid["locked"] = True
    stop_raid_views(raid["signup_message_id"])


async def finish_raid_cleanup(guild, raid: dict) -> tuple[bool, list[str]]:
    if guild is None and any(player.get("user_id") for player in raid["signups"].values()):
        return False, ["I could not access the server. Role cleanup remains saved for another attempt."]
    warnings = await remove_all_raid_roles(guild, raid)
    if warnings:
        return False, [*warnings, "The posts were deleted. Role cleanup is saved and will retry automatically."]
    await delete_raid_row(raid["signup_message_id"])
    return True, []


# --- Scheduling and input validation ---

def first_sunday(year: int, month: int):
    first_day = datetime(year, month, 1).date()
    days_until_sunday = (6 - first_day.weekday()) % 7
    return first_day + timedelta(days=days_until_sunday)


def second_sunday(year: int, month: int):
    return first_sunday(year, month) + timedelta(days=7)


def us_daylight_transitions(year: int) -> tuple[datetime, datetime]:
    # US daylight-saving rules in effect since 2007.
    return (
        datetime.combine(second_sunday(year, 3), datetime_time(hour=2)),
        datetime.combine(first_sunday(year, 11), datetime_time(hour=2)),
    )


def timezone_for_local_datetime(timezone_name: str, local_datetime: datetime):
    if timezone_name not in STANDARD_OFFSETS:
        raise ValueError("That timezone is not available.")

    offset_hours = STANDARD_OFFSETS[timezone_name]
    if timezone_name in DST_OFFSETS:
        dst_start, dst_end = us_daylight_transitions(local_datetime.year)
        if dst_start <= local_datetime < dst_start + timedelta(hours=1):
            raise ValueError("That local time does not exist because clocks move forward. Choose a time before 2 AM or at or after 3 AM.")
        if dst_end - timedelta(hours=1) <= local_datetime < dst_end:
            raise ValueError("That local time occurs twice because clocks move back. Choose a time before 1 AM or at or after 2 AM.")
        if dst_start < local_datetime < dst_end:
            offset_hours = DST_OFFSETS[timezone_name]

    return timezone(timedelta(hours=offset_hours), name=timezone_name)


def parse_raid_date(date_text: str, timezone_name: str = DEFAULT_TIMEZONE_NAME):
    clean_date = " ".join(date_text.strip().replace("-", "/").split())
    if timezone_name not in STANDARD_OFFSETS:
        raise ValueError("That timezone is not available.")

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

    now = datetime.now(timezone.utc)
    offset_hours = STANDARD_OFFSETS[timezone_name]
    if timezone_name in DST_OFFSETS:
        dst_start, dst_end = us_daylight_transitions(now.year)
        dst_start_utc = dst_start.replace(tzinfo=timezone.utc) - timedelta(hours=offset_hours)
        dst_end_utc = dst_end.replace(tzinfo=timezone.utc) - timedelta(hours=DST_OFFSETS[timezone_name])
        if dst_start_utc <= now < dst_end_utc:
            offset_hours = DST_OFFSETS[timezone_name]
    today = now.astimezone(timezone(timedelta(hours=offset_hours))).date()
    if clean_date.casefold() == "today":
        return today

    date_formats_without_year = (
        "%m/%d",
        "%B %d",
        "%b %d",
        "%B %d,",
        "%b %d,",
    )
    for date_format in date_formats_without_year:
        try:
            # Parse month/day independently, then use the raid timezone's current year.
            parsed = datetime.strptime(f"{clean_date} 2000", f"{date_format} %Y").date()
        except ValueError:
            continue
        try:
            return parsed.replace(year=today.year)
        except ValueError:
            raise ValueError(f"That date does not exist in {today.year}. Include the intended year, such as `2028-02-29`.") from None

    raise ValueError("Use `today` or a date like `5/25`, `May 25`, or `2026-05-25`.")


def parse_raid_datetime(date_text: str, time_text: str, timezone_name: str) -> int:
    parsed_date = parse_raid_date(date_text, timezone_name)

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


# --- Roster rendering and posts ---

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

    embed.add_field(name="__**Tanks**__", value=fit_embed_lines(tank_lines, "OPEN"), inline=False)
    embed.add_field(name="__**Healers**__", value=fit_embed_lines(healer_lines, "OPEN"), inline=False)
    embed.add_field(name="__**DPS**__", value=fit_embed_lines(dps_lines, "OPEN"), inline=False)
    open_slot_lines = []
    for role_keys in ROSTER_GROUPS:
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
        signup_message = channel.get_partial_message(raid["signup_message_id"])
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
        roster_message = channel.get_partial_message(roster_message_id)
        await roster_message.edit(embed=roster_embed(raid))
    except discord.NotFound:
        logger.warning("Roster message %s no longer exists", roster_message_id)
        updated = False
    except discord.HTTPException:
        logger.exception("Could not update roster message %s", roster_message_id)
        updated = False

    return updated


async def fetch_raid_channel(raid: dict):
    if not raid.get("channel_id"):
        return None
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
            message = channel.get_partial_message(message_id)
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


async def resolve_raid_for_command(
    interaction: discord.Interaction,
    signup_message_id: str | None,
    *,
    allow_cleanup_pending: bool = False,
):
    if interaction.channel is None or interaction.guild is None:
        return None, "This command needs to be used in a server channel."

    if signup_message_id:
        try:
            message_id = int(signup_message_id.strip())
            if not 0 < message_id <= 2**63 - 1:
                raise ValueError
            raid = await fetch_signup(message_id)
        except ValueError:
            return None, "That signup message ID does not look valid."
    else:
        raid = await fetch_latest_signup_for_channel(interaction.channel_id)

    if not raid:
        return None, "I could not find a raid signup."

    if raid.get("channel_id") != interaction.channel_id:
        return None, "That signup belongs to a different channel."

    if raid.get("cleanup_pending") and not allow_cleanup_pending:
        return None, "That raid's posts were deleted and role cleanup is pending. It will retry automatically; use a deletion command with its signup ID to retry now."

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

        if not current_raid.get("cleanup_pending"):
            if not await delete_raid_messages(interaction.channel, current_raid):
                return False, ["I could not delete every bot post, so the saved roster was kept for another attempt."]
            await mark_raid_cleanup_pending(current_raid)
        return await finish_raid_cleanup(interaction.guild, current_raid)


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
        if scheduled_at <= int(datetime.now(timezone.utc).timestamp()):
            raise ValueError("Choose a future raid date and time in Pacific Time (PT). Dates without a year use the current year; include the year for a later year.")
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

    existing_role = find_role_by_name(interaction.guild, DEFAULT_RAID_ROLE_NAME)
    if existing_role:
        raid["raid_role_id"] = existing_role.id

    setup_warnings = []
    try:
        signup_message = await interaction.channel.send(embed=signup_embed(raid))
        raid["signup_message_id"] = signup_message.id
    except discord.HTTPException as exc:
        logger.error(
            "Could not create signup message",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        await interaction.followup.send(
            "I could not create the signup post. Check my View Channel, Send Messages, and Embed Links permissions.",
            ephemeral=True,
        )
        return

    # Keep setup saves from overwriting commands or reminders that see the new row.
    async with get_raid_lock(signup_message.id):
        try:
            await insert_raid(raid)
        except aiosqlite.Error as exc:
            await delete_created_messages([signup_message], "database failure")
            logger.error(
                "Could not save new raid %s",
                raid["signup_message_id"],
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.followup.send("The raid signup could not be saved. Please try again.", ephemeral=True)
            return

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
                setup_warnings.append(f"I could not find a role named `{DEFAULT_NOTIFY_ROLE_NAME}` to ping.")

            try:
                announcement_message = await interaction.channel.send(
                    f"{announcement_prefix}{announcement_text}\n\nRole signup: {signup_message.jump_url}",
                    allowed_mentions=allowed_mentions,
                )
                raid["announcement_message_id"] = announcement_message.id
                await save_raid_state(raid)
            except discord.HTTPException as exc:
                logger.error(
                    "Could not create announcement message for %s",
                    raid["signup_message_id"],
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                setup_warnings.append("The signup was saved, but I could not send the announcement message.")
            except aiosqlite.Error as exc:
                await delete_created_messages([announcement_message], "announcement database update failed")
                raid["announcement_message_id"] = None
                logger.error(
                    "Could not save announcement message for %s",
                    raid["signup_message_id"],
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                setup_warnings.append("The signup was saved, but I could not save the announcement message.")

        try:
            roster_message = await interaction.channel.send(embed=roster_embed(raid))
            raid["roster_message_id"] = roster_message.id
            await save_raid_state(raid)
        except discord.HTTPException as exc:
            logger.error(
                "Could not create roster message for %s",
                raid["signup_message_id"],
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            setup_warnings.append("The signup was saved, but I could not create the roster message.")
        except aiosqlite.Error as exc:
            await delete_created_messages([roster_message], "roster database update failed")
            raid["roster_message_id"] = 0
            logger.error(
                "Could not save roster message for %s",
                raid["signup_message_id"],
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            setup_warnings.append("The signup was saved, but I could not save the roster message.")

        try:
            await signup_message.edit(embed=signup_embed(raid), view=RaidSignupView(raid))
        except discord.HTTPException:
            logger.exception("Could not activate signup buttons for %s", raid["signup_message_id"])
            setup_warnings.append("I created the signup but could not activate its buttons.")

    response = f"Raid signup created. Signup message ID: `{raid['signup_message_id']}`"
    if setup_warnings:
        response += "\n" + "\n".join(f"- {warning}" for warning in setup_warnings)
    await interaction.followup.send(response, ephemeral=True)


# --- Signup interactions ---

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

            if raid.get("cleanup_pending"):
                await interaction.followup.send("This raid is being deleted; role cleanup is pending.", ephemeral=True)
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


def standby_panel_text(raid: dict, roles: list[str]) -> str:
    selected = standby_role_text(roles) if roles else "None"
    return (
        f"Standby roles for **{raid_title(raid)}**: **{selected}**\n"
        "Toggle any roles you can cover, then press **Confirm Standby** to save. "
        "Confirm with no roles selected to leave standby. Closing this picker keeps your saved roles unchanged."
    )


class StandbyRoleView(ErrorHandledView):
    def __init__(self, signup_message_id: int, user_id: int, roles: list[str] | None = None):
        super().__init__(timeout=300)
        self.signup_message_id = signup_message_id
        self.user_id = user_id
        self.roles = [role for role in STANDBY_CHOICES if role in (roles or [])]
        self.add_role_buttons()
        self.add_confirm_button()
        self.add_clear_button()

    def add_role_buttons(self):
        for role_name in STANDBY_CHOICES:
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

    async def fetch_editable_raid(self, interaction: discord.Interaction):
        if self.is_finished():
            await interaction.followup.send("This standby picker is closed. Open Standby again to make changes.", ephemeral=True)
            return None
        raid = await fetch_signup(self.signup_message_id)
        if not raid:
            await interaction.followup.send("I could not find this raid signup anymore.", ephemeral=True)
        elif raid["locked"]:
            await interaction.followup.send("This roster is locked.", ephemeral=True)
        elif user_is_signed_up(raid, interaction.user.id):
            await interaction.followup.send("You already have a main role. Resign first if you want to move to standby.", ephemeral=True)
        else:
            return raid
        return None

    async def toggle_role(self, interaction: discord.Interaction, role_name: str):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This standby picker is not yours.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=False)
        async with get_raid_lock(self.signup_message_id):
            raid = await self.fetch_editable_raid(interaction)
            if not raid:
                return
            if role_name in self.roles:
                self.roles.remove(role_name)
            else:
                self.roles.append(role_name)
            await interaction.edit_original_response(content=standby_panel_text(raid, self.roles), view=self)

    async def confirm_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This standby picker is not yours.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=False)
        async with get_raid_lock(self.signup_message_id):
            raid = await self.fetch_editable_raid(interaction)
            if not raid:
                return

            user_key = str(interaction.user.id)
            saved_entry = raid["standby"].get(user_key)
            entry = None
            if self.roles:
                entry = player_from_user(interaction.user) | {
                    "roles": self.roles.copy(),
                    "needs_mount": saved_entry.get("needs_mount") if saved_entry else None,
                }
                raid["standby"][user_key] = entry
                await save_raid_state(raid)
            elif remove_user_from_standby(raid["standby"], interaction.user.id):
                await save_raid_state(raid)
            if entry or saved_entry:
                await update_raid_messages(interaction.channel, raid)
            self.stop()

        await interaction.edit_original_response(
            content=f"Standby roles confirmed: **{standby_role_text(entry['roles'])}**." if entry else "Standby cleared.",
            view=None,
        )
        if entry and entry.get("needs_mount") is None:
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
            raid = await self.fetch_editable_raid(interaction)
            if not raid:
                return
            self.roles.clear()
            await interaction.edit_original_response(content=standby_panel_text(raid, self.roles), view=self)


class RaidSignupView(ErrorHandledView):
    def __init__(self, raid: dict):
        super().__init__(timeout=None)
        self.signup_message_id = raid["signup_message_id"]
        self.add_role_buttons(raid)
        self.add_standby_button(raid)
        self.add_resign_button(raid)
        self.add_mount_button()

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

    def add_mount_button(self):
        button = discord.ui.Button(
            label="Mount Status",
            style=discord.ButtonStyle.secondary,
            custom_id=f"raid_signup:{self.signup_message_id}:mount",
            row=4,
        )
        button.callback = self.mount_callback
        self.add_item(button)

    async def mount_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        raid = await fetch_signup(self.signup_message_id)
        if not raid:
            await interaction.followup.send("I could not find this raid signup anymore.", ephemeral=True)
            return

        if raid.get("cleanup_pending"):
            await interaction.followup.send("This raid is being deleted; role cleanup is pending.", ephemeral=True)
            return

        if not user_is_signed_up(raid, interaction.user.id) and str(interaction.user.id) not in raid["standby"]:
            await interaction.followup.send("You are not on this roster or standby list.", ephemeral=True)
            return

        await interaction.followup.send(
            "Do you still need the mount?",
            view=MountNeedView(self.signup_message_id, interaction.user.id),
            ephemeral=True,
        )

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

        entry = raid["standby"].get(str(interaction.user.id))
        view = StandbyRoleView(self.signup_message_id, interaction.user.id, entry["roles"] if entry else [])
        await interaction.followup.send(
            standby_panel_text(raid, view.roles),
            view=view,
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


# --- Slash commands ---

@tree.command(name="raidsetup", description="Create a raid signup with an announcement message and notification ping")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(
    raid_name="Name of the raid",
    date="Date in Pacific Time: today, 5/25, May 25, or 2026-05-25. No year means this year.",
    time="Raid time in Pacific Time (PT), such as 20:00, 8:00 PM, or 8PM",
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
    date="Date in Pacific Time: today, 5/25, May 25, or 2026-05-25. No year means this year.",
    time="Raid time in Pacific Time (PT), such as 20:00, 8:00 PM, or 8PM",
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
@app_commands.guild_only()
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
@app_commands.guild_only()
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
@app_commands.guild_only()
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
@app_commands.guild_only()
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
@app_commands.guild_only()
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
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True, manage_roles=True)
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

    if not interaction.user.guild_permissions.manage_roles:
        await interaction.followup.send("You need Manage Roles permission to choose a signup role.", ephemeral=True)
        return
    if interaction.user.id != interaction.guild.owner_id and role >= interaction.user.top_role:
        await interaction.followup.send("Choose a role below your highest server role.", ephemeral=True)
        return

    safety_error = raid_role_safety_error(role)
    if safety_error:
        await interaction.followup.send(safety_error, ephemeral=True)
        return

    bot_member = interaction.guild.me
    if bot_member is None or role >= bot_member.top_role:
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

        for player in raid["signups"].values():
            try:
                member = await fetch_guild_member(interaction.guild, player.get("user_id"))
            except discord.HTTPException:
                logger.exception("Could not fetch guild member %s for raid role change", player.get("user_id"))
                warning = "I could not check one or more saved players, so their raid roles may still need updating."
                if warning not in warnings:
                    warnings.append(warning)
                continue
            if member:
                warning = await add_raid_role(member, raid)
                if warning and warning not in warnings:
                    warnings.append(warning)
                if warning is None and old_role and old_role.id != role.id:
                    warning = await remove_raid_role(member, old_role_raid)
                    if warning and warning not in warnings:
                        warnings.append(warning)

        await update_raid_messages(interaction.channel, raid)

    message = f"Raid role set to {role.mention} for **{raid_title(raid)}**."
    if warnings:
        message += "\n" + "\n".join(warnings)

    await interaction.followup.send(message, ephemeral=True)


@tree.command(name="bumproster", description="Move the roster post to the bottom of the channel")
@app_commands.guild_only()
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
                old_roster_message = interaction.channel.get_partial_message(old_roster_message_id)
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
@app_commands.guild_only()
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


async def delete_raid_command(interaction: discord.Interaction, signup_message_id: str | None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    allowed, error = require_roster_manager(interaction)
    if not allowed:
        await interaction.followup.send(error, ephemeral=True)
        return

    raid, error = await resolve_raid_for_command(interaction, signup_message_id, allow_cleanup_pending=True)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    deleted, warnings = await delete_raid_for_command(interaction, raid)
    message = f"Deleted **{raid_title(raid)}**." if deleted else f"Could not fully delete **{raid_title(raid)}**."
    if warnings:
        message += "\n" + "\n".join(warnings)
    await interaction.followup.send(message, ephemeral=True)


@tree.command(name="deleteroster", description="Clear a roster, remove raid roles, and delete bot posts")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(signup_message_id="Optional signup message ID. Leave blank for newest signup in this channel.")
async def deleteroster(interaction: discord.Interaction, signup_message_id: str | None = None):
    await delete_raid_command(interaction, signup_message_id)


@tree.command(name="deleteraid", description="Delete a raid signup, roster post, and database entry")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(signup_message_id="Optional signup message ID. Leave blank for newest signup in this channel.")
async def deleteraid(interaction: discord.Interaction, signup_message_id: str | None = None):
    await delete_raid_command(interaction, signup_message_id)


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
            "Creates a signup and roster using a preset raid name, such as Everkeep EX, M4S, or FRU. "
            "For an announcement ping and meeting server, use `/raidsetup`. Requires Manage Messages."
        ),
        inline=False,
    )
    embed.add_field(
        name="Dates and Times",
        value=(
            "Enter dates and times in Pacific Time (PT). `today` uses the Pacific calendar date. "
            "A date without a year, such as `10/24`, uses the current year. "
            "Past dates/times return an error; include the year to schedule for a later year. "
            "Posted Discord timestamps display in each viewer's timezone."
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
            " Use `Mount Status` to answer again or correct a previous answer."
        ),
        inline=False,
    )
    embed.add_field(
        name="Standby List",
        value=(
            "Click `Standby`, toggle every role type you can cover, then press `Confirm Standby`. "
            "Changes are saved only on confirmation; closing the picker keeps your saved roles. "
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
            "`/deleteraid` is an alias for `/deleteroster`. Both include the announcement post. Requires Manage Messages."
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
            "Requires Manage Messages and Manage Roles. Choose an ordinary role below your highest role "
            "and the bot's role. The server owner is exempt from the caller hierarchy check."
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
            "for reminders and cleanup. Failed role cleanup after deletion is saved and retried each minute."
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


# --- Reminders and cleanup ---

def reminder_work_due(raid: dict, now: int) -> bool:
    if raid.get("cleanup_pending"):
        return True
    scheduled_at = raid["scheduled_at"]
    if now >= scheduled_at + (2 * 60 * 60):
        return True
    if now > scheduled_at:
        return False
    if now >= scheduled_at - FIVE_MINUTE_REMINDER_SECONDS:
        return not raid["reminders_sent"].get("five_minute")

    offset = raid.get("reminder_offset")
    if offset is None:
        return choose_reminder_offset(scheduled_at, now=now) is not None
    return not raid["reminders_sent"].get("automatic") and now >= scheduled_at - offset


async def send_due_reminders():
    now = int(datetime.now(timezone.utc).timestamp())
    for snapshot in await fetch_scheduled_raids(now):
        try:
            if reminder_work_due(snapshot, now):
                await process_due_raid(snapshot["signup_message_id"], now)
        except Exception:
            logger.exception("Reminder processing failed for %s", snapshot["signup_message_id"])


async def process_due_raid(signup_message_id: int, now: int):
    async with get_raid_lock(signup_message_id):
        raid = await fetch_signup(signup_message_id)
        if not raid:
            return

        scheduled_at = raid.get("scheduled_at")
        if scheduled_at is None and not raid.get("cleanup_pending"):
            return

        if raid.get("cleanup_pending") or now >= scheduled_at + (2 * 60 * 60):
            channel = await fetch_raid_channel(raid)
            if channel is None:
                logger.warning("Post-raid cleanup cannot access channel for %s", signup_message_id)
                return

            if not raid.get("cleanup_pending"):
                if not await delete_raid_messages(channel, raid):
                    logger.warning("Post-raid cleanup could not delete all messages for %s", signup_message_id)
                    return
                await mark_raid_cleanup_pending(raid)
            deleted, warnings = await finish_raid_cleanup(getattr(channel, "guild", None), raid)
            for warning in warnings:
                logger.warning("Post-raid cleanup for %s: %s", signup_message_id, warning)
            if deleted:
                logger.info("Completed post-raid cleanup for %s", signup_message_id)
            return

        if now > scheduled_at:
            return

        reminder_offset = raid.get("reminder_offset")
        if reminder_offset is None:
            reminder_offset = choose_reminder_offset(scheduled_at, now=now)
        if raid.get("reminder_offset") != reminder_offset:
            raid["reminder_offset"] = reminder_offset
            await save_reminder_state(raid)

        five_minute_due = (
            not raid["reminders_sent"].get("five_minute")
            and scheduled_at - FIVE_MINUTE_REMINDER_SECONDS <= now <= scheduled_at
        )
        if five_minute_due:
            channel = await fetch_raid_channel(raid)
            if channel is None:
                logger.warning("Five-minute reminder cannot access channel for %s", signup_message_id)
                return

            try:
                await channel.send(
                    five_minute_reminder_text(channel, raid),
                    allowed_mentions=allowed_mentions_for_raid(channel, raid),
                )
            except discord.HTTPException:
                logger.exception("Could not send five-minute reminder for %s", signup_message_id)
                return

            raid["reminders_sent"]["five_minute"] = True
            await save_reminder_state(raid)
            return

        if now >= scheduled_at - FIVE_MINUTE_REMINDER_SECONDS:
            return

        if not reminder_offset or raid["reminders_sent"].get("automatic"):
            return

        if now < scheduled_at - reminder_offset:
            return

        channel = await fetch_raid_channel(raid)
        if channel is None:
            logger.warning("Automatic reminder cannot access channel for %s", signup_message_id)
            return

        try:
            ping_text = reminder_ping_text(channel, raid)
            await channel.send(
                f"Reminder: **{raid_title(raid)}** starts <t:{scheduled_at}:R>.\n"
                f"{schedule_text(raid)}\n"
                f"Ping: {ping_text}",
                allowed_mentions=allowed_mentions_for_raid(channel, raid),
            )
        except discord.HTTPException:
            logger.exception("Could not send automatic reminder for %s", signup_message_id)
            return

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


# --- Error reporting and entry point ---

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
    logger.info("Bot ready! Logged in as %s", bot.user)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set.")
    bot.run(token)
