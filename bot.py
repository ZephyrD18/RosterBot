import ast
import asyncio
import json
import os
from datetime import datetime, time as datetime_time, timedelta, timezone

import aiosqlite
import discord
from discord import app_commands


intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

DB_PATH = "raids.db"
views_loaded = False
reminder_task = None
DEFAULT_RAID_ROLE_NAME = "FFXIVActiveRoster"
DEFAULT_NOTIFY_ROLE_NAME = "FFXIV Raid"
DEFAULT_TIMEZONE_NAME = "PT"

ROLE_CHOICES = [
    app_commands.Choice(name="Main Tank", value="MT"),
    app_commands.Choice(name="Off Tank", value="OT"),
    app_commands.Choice(name="Healer 1", value="H1"),
    app_commands.Choice(name="Healer 2", value="H2"),
    app_commands.Choice(name="Melee 1", value="M1"),
    app_commands.Choice(name="Melee 2", value="M2"),
    app_commands.Choice(name="Ranged 1", value="R1"),
    app_commands.Choice(name="Ranged 2", value="R2"),
]

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
ROLE_CATEGORIES = {key: category for key, _, _, _, category in ROLES}
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


def decode_waitlist(raw: str | None) -> dict:
    value = decode_json(raw, {})
    return {role_key: players for role_key, players in value.items() if isinstance(players, list)}


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


def decode_confirmations(raw: str | None) -> list[int]:
    value = decode_json(raw, [])
    confirmations = []
    for user_id in value:
        try:
            confirmations.append(int(user_id))
        except (TypeError, ValueError):
            pass
    return confirmations


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


def remove_user_from_standby(standby: dict, user_id: int):
    standby.pop(str(user_id), None)


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


async def remove_raid_role(member: discord.Member, raid: dict):
    role = raid_role_for_guild(member.guild, raid)
    if role is None or role not in member.roles:
        return

    try:
        await member.remove_roles(role, reason=f"Left {raid_title(raid)}")
    except discord.Forbidden:
        pass


async def remove_raid_role_from_player(guild: discord.Guild | None, raid: dict, player: dict):
    if guild is None or not player.get("user_id"):
        return

    member = guild.get_member(int(player["user_id"]))
    if member is None:
        try:
            member = await guild.fetch_member(int(player["user_id"]))
        except (discord.Forbidden, discord.NotFound):
            return

    await remove_raid_role(member, raid)


def reminder_ping_text(channel, raid: dict) -> str:
    guild = getattr(channel, "guild", None)
    role = raid_role_for_guild(guild, raid)
    if role:
        return role.mention

    return signed_player_mentions(raid["signups"])


def require_roster_manager(interaction: discord.Interaction) -> tuple[bool, str | None]:
    if not isinstance(interaction.user, discord.Member):
        return False, "I could not confirm your server permissions."

    permissions = interaction.user.guild_permissions
    if permissions.manage_messages or permissions.administrator:
        return True, None

    return False, "You need Manage Messages permission to manage raid rosters."


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
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
                notify_role_name TEXT
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
        )
        for column_name, column_type in migrations:
            if column_name not in columns:
                await db.execute(f"ALTER TABLE signups ADD COLUMN {column_name} {column_type}")

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
        "waitlist": decode_waitlist(row[7]),
        "standby": decode_standby(row[8]),
        "confirmations": decode_confirmations(row[9]),
        "scheduled_at": row[10],
        "timezone": row[11],
        "locked": bool(row[12]),
        "role_restrictions": bool(row[13]),
        "group_name": row[14],
        "reminders_sent": decode_reminders(row[15]),
        "reminder_offset": row[16],
        "raid_role_id": row[17],
        "raid_role_name": row[18],
        "announcement_message_id": row[19],
        "setup_message": row[20],
        "notify_role_id": row[21],
        "notify_role_name": row[22],
    }


async def fetch_signup(signup_message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT signup_message_id, roster_message_id, channel_id, raid_name, date, time,
                   signups, waitlist, standby, confirmations, scheduled_at, timezone, locked,
                   role_restrictions, group_name, reminders_sent, reminder_offset,
                   raid_role_id, raid_role_name, announcement_message_id, setup_message,
                   notify_role_id, notify_role_name
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
                   notify_role_id, notify_role_name
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
            SET roster_message_id = ?, signups = ?, waitlist = ?, standby = ?, confirmations = ?, locked = ?,
                role_restrictions = ?, group_name = ?, reminders_sent = ?, reminder_offset = ?,
                raid_role_id = ?, raid_role_name = ?, announcement_message_id = ?,
                setup_message = ?, notify_role_id = ?, notify_role_name = ?
            WHERE signup_message_id = ?
            """,
            (
                raid.get("roster_message_id"),
                encode_json(raid["signups"]),
                encode_json(raid["waitlist"]),
                encode_json(raid["standby"]),
                encode_json(raid["confirmations"]),
                int(raid["locked"]),
                int(raid["role_restrictions"]),
                raid.get("group_name"),
                encode_json(raid["reminders_sent"]),
                raid.get("reminder_offset"),
                raid.get("raid_role_id"),
                raid.get("raid_role_name"),
                raid.get("announcement_message_id"),
                raid.get("setup_message"),
                raid.get("notify_role_id"),
                raid.get("notify_role_name"),
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
    if raid.get("scheduled_at"):
        return f"**When:** <t:{raid['scheduled_at']}:F>\n**Countdown:** <t:{raid['scheduled_at']}:R>"

    return f"**Date:** {raid['date']}\n**Gather Time:** {raid['time']}"


def choose_reminder_offset(scheduled_at: int) -> int | None:
    seconds_until_raid = scheduled_at - int(datetime.now(timezone.utc).timestamp())
    for seconds_before, _ in REMINDER_WINDOWS:
        if seconds_until_raid >= seconds_before:
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


def signup_embed(raid: dict) -> discord.Embed:
    status = []
    if raid["locked"]:
        status.append("Roster locked")
    if raid["role_restrictions"]:
        status.append("Role restrictions on")
    if raid.get("reminder_offset"):
        status.append(f"{reminder_label(raid['reminder_offset'])} reminder")
    if raid.get("raid_role_name"):
        status.append(f"{raid['raid_role_name']} active roster role")

    status_text = f"\n**Status:** {', '.join(status)}" if status else ""
    setup_message = raid.get("setup_message")
    setup_text = f"\n\n{setup_message}" if setup_message else ""
    embed = discord.Embed(
        title=f"⚔️ {raid_title(raid)}",
        description=(
            f"{schedule_text(raid)}{status_text}{setup_text}\n\n"
            "Choose your light party role below."
        ),
        color=0x3E6AE1,
    )
    embed.set_footer(text=f"Signup message ID: {raid['signup_message_id']}")
    return embed


def roster_group_field(raid: dict, title: str, role_keys: tuple[str, ...], limit: int):
    filled = sum(1 for role_key in role_keys if role_key in raid["signups"])
    lines = []
    for index, role_key in enumerate(role_keys, start=1):
        player = raid["signups"].get(role_key)
        lines.append(f"`{index}` **{ROLE_LABELS[role_key]}:** {roster_player_text(player)}")

    return f"{title} {filled}/{limit}", "\n".join(lines)


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
        value="\n".join(standby_lines) if standby_lines else "No standby players.",
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


def user_has_required_role(member: discord.Member, role_key: str) -> bool:
    category = ROLE_CATEGORIES[role_key]
    accepted_names = {category.lower()}
    if category == "DPS":
        accepted_names.update({"melee", "ranged", "range"})

    member_roles = {role.name.lower() for role in member.roles}
    return bool(accepted_names & member_roles)


async def update_raid_messages(channel, raid: dict):
    if channel is None:
        return False

    view = RaidSignupView(raid)

    try:
        signup_message = await channel.fetch_message(raid["signup_message_id"])
        await signup_message.edit(embed=signup_embed(raid), view=view)
    except (discord.Forbidden, discord.NotFound):
        return False

    roster_message_id = raid.get("roster_message_id")
    if not roster_message_id:
        return True

    try:
        roster_message = await channel.fetch_message(roster_message_id)
    except (discord.Forbidden, discord.NotFound):
        return True

    await roster_message.edit(embed=roster_embed(raid))
    return True


async def fetch_raid_channel(raid: dict):
    channel = bot.get_channel(raid["channel_id"])
    if channel is not None:
        return channel

    try:
        return await bot.fetch_channel(raid["channel_id"])
    except (discord.Forbidden, discord.NotFound):
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
        except discord.Forbidden:
            deleted = False

    return deleted


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


async def create_raid(
    interaction: discord.Interaction,
    raid_name: str,
    date: str,
    time: str,
    group_name: str | None = None,
    role_restrictions: bool = False,
    raid_role: discord.Role | None = None,
    setup_message: str | None = None,
    notify_role: discord.Role | None = None,
    send_announcement: bool = False,
):
    if interaction.channel is None:
        await interaction.followup.send("This command needs to be used in a server channel.", ephemeral=True)
        return

    try:
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
        "waitlist": {},
        "standby": {},
        "confirmations": [],
        "scheduled_at": scheduled_at,
        "timezone": DEFAULT_TIMEZONE_NAME,
        "locked": False,
        "role_restrictions": role_restrictions,
        "group_name": group_name,
        "reminders_sent": {},
        "reminder_offset": choose_reminder_offset(scheduled_at),
        "raid_role_id": raid_role.id if raid_role else None,
        "raid_role_name": raid_role.name if raid_role else DEFAULT_RAID_ROLE_NAME,
        "announcement_message_id": None,
        "setup_message": clean_setup_message(setup_message),
        "notify_role_id": notify_role.id if notify_role else None,
        "notify_role_name": notify_role.name if notify_role else None,
    }

    if raid_role is None and interaction.guild is not None:
        existing_role = find_role_by_name(interaction.guild, DEFAULT_RAID_ROLE_NAME)
        if existing_role:
            raid["raid_role_id"] = existing_role.id

    signup_message = await interaction.channel.send(embed=signup_embed(raid))
    raid["signup_message_id"] = signup_message.id
    roster_message = await interaction.channel.send(embed=roster_embed(raid))
    raid["roster_message_id"] = roster_message.id

    announcement_warning = None
    if send_announcement:
        announcement_role = notify_role
        if announcement_role is None and interaction.guild is not None:
            announcement_role = find_role_by_name(interaction.guild, DEFAULT_NOTIFY_ROLE_NAME)

        if announcement_role is not None:
            raid["notify_role_id"] = announcement_role.id
            raid["notify_role_name"] = announcement_role.name
            announcement_text = raid.get("setup_message") or default_setup_message(raid["raid_name"])
            announcement_message = await interaction.channel.send(
                f"{announcement_role.mention} {announcement_text}\n\nRole signup: {signup_message.jump_url}",
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
            raid["announcement_message_id"] = announcement_message.id
        else:
            announcement_warning = f" I could not find a role named `{DEFAULT_NOTIFY_ROLE_NAME}` to ping."

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO signups (
                signup_message_id, roster_message_id, channel_id, raid_name, date, time,
                signups, waitlist, standby, confirmations, scheduled_at, timezone, locked,
                role_restrictions, group_name, reminders_sent, reminder_offset,
                raid_role_id, raid_role_name, announcement_message_id, setup_message,
                notify_role_id, notify_role_name
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raid["signup_message_id"],
                raid["roster_message_id"],
                raid["channel_id"],
                raid["raid_name"],
                raid["date"],
                raid["time"],
                encode_json(raid["signups"]),
                encode_json(raid["waitlist"]),
                encode_json(raid["standby"]),
                encode_json(raid["confirmations"]),
                raid["scheduled_at"],
                raid["timezone"],
                int(raid["locked"]),
                int(raid["role_restrictions"]),
                raid["group_name"],
                encode_json(raid["reminders_sent"]),
                raid["reminder_offset"],
                raid["raid_role_id"],
                raid["raid_role_name"],
                raid["announcement_message_id"],
                raid["setup_message"],
                raid["notify_role_id"],
                raid["notify_role_name"],
            ),
        )
        await db.commit()

    await update_raid_messages(interaction.channel, raid)
    await interaction.followup.send(
        f"Raid signup created. Signup message ID: `{raid['signup_message_id']}`{announcement_warning or ''}",
        ephemeral=True,
    )


class MountNeedView(discord.ui.View):
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


class StandbyRoleView(discord.ui.View):
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
            await interaction.followup.send("You already have a main role, so standby was cleared.", ephemeral=True)
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
        raid = await fetch_signup(self.signup_message_id)
        if not raid:
            await interaction.followup.send("I could not find this raid signup anymore.", ephemeral=True)
            return

        remove_user_from_standby(raid["standby"], interaction.user.id)
        await save_raid_state(raid)
        await update_raid_messages(interaction.channel, raid)
        await interaction.edit_original_response(content=standby_panel_text(raid, interaction.user.id), view=self)


class RaidSignupView(discord.ui.View):
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

        raid = await fetch_signup(self.signup_message_id)
        if not raid:
            await interaction.followup.send("I could not find this raid signup anymore.", ephemeral=True)
            return

        if raid["locked"]:
            await interaction.followup.send("This roster is locked.", ephemeral=True)
            return

        if raid["role_restrictions"]:
            if not isinstance(interaction.user, discord.Member):
                await interaction.followup.send("I could not confirm your server roles.", ephemeral=True)
                return
            if not user_has_required_role(interaction.user, role_key):
                required = ROLE_CATEGORIES[role_key]
                await interaction.followup.send(
                    f"You need a `{required}` Discord role to take **{role_label}**.",
                    ephemeral=True,
                )
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
        role_warning = None
        if isinstance(interaction.user, discord.Member):
            role_warning = await add_raid_role(interaction.user, raid)

        await save_raid_state(raid)
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

        raid = await fetch_signup(self.signup_message_id)
        if not raid:
            await interaction.followup.send("I could not find this raid signup anymore.", ephemeral=True)
            return

        if raid["locked"]:
            await interaction.followup.send("This roster is locked.", ephemeral=True)
            return

        user_id = interaction.user.id
        for role_key, player in list(raid["signups"].items()):
            if player.get("user_id") == user_id:
                del raid["signups"][role_key]
                raid["confirmations"] = [uid for uid in raid["confirmations"] if uid != user_id]
                if isinstance(interaction.user, discord.Member):
                    await remove_raid_role(interaction.user, raid)
                await save_raid_state(raid)
                await self.refresh_messages(interaction, raid)

                role_label = ROLE_LABELS.get(role_key, role_key)
                await interaction.followup.send(
                    f"You have resigned the position of **{role_label}**.",
                    ephemeral=True,
                )
                return

        await interaction.followup.send("You're not registered for any role.", ephemeral=True)

    async def refresh_messages(self, interaction: discord.Interaction, raid: dict):
        channel = interaction.channel
        if channel is None and raid.get("channel_id"):
            channel = bot.get_channel(raid["channel_id"])

        await update_raid_messages(channel, raid)


@tree.command(name="raidsetup", description="Create a raid signup with a custom message and raid notification ping")
@app_commands.describe(
    raid_name="Name of the raid",
    date="Raid date, such as 5/25, May 25, or 2026-05-25",
    time="Raid time, such as 20:00, 8:00 PM, 8PM, or 8 PM",
    message="Optional message shown in the signup and sent with the notification ping",
    notify_role=f"Optional role to ping. Defaults to {DEFAULT_NOTIFY_ROLE_NAME}.",
    group="Optional group or team name, such as Group A",
)
async def raidsetup(
    interaction: discord.Interaction,
    raid_name: str,
    date: str,
    time: str,
    message: str | None = None,
    notify_role: discord.Role | None = None,
    group: str | None = None,
):
    await interaction.response.defer(ephemeral=True, thinking=True)
    setup_message = clean_setup_message(message) or default_setup_message(raid_name)
    await create_raid(
        interaction,
        raid_name,
        date,
        time,
        group,
        False,
        None,
        setup_message,
        notify_role,
        True,
    )


@tree.command(name="raidsignup", description="Create a custom raid signup")
@app_commands.describe(
    raid_name="Name of the raid",
    date="Raid date, such as 5/25, May 25, or 2026-05-25",
    time="Raid time, such as 20:00, 8:00 PM, 8PM, or 8 PM",
    group="Optional group or team name, such as Group A",
)
async def raidsignup(
    interaction: discord.Interaction,
    raid_name: str,
    date: str,
    time: str,
    group: str | None = None,
):
    await interaction.response.defer(ephemeral=True, thinking=True)
    await create_raid(interaction, raid_name, date, time, group, False, None)


@tree.command(name="raidpreset", description="Create a raid signup from a preset raid name")
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
    await create_raid(interaction, preset.value, date, time, group, False, None)


@tree.command(name="lockroster", description="Lock a roster so players cannot change roles")
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

    raid["locked"] = True
    await save_raid_state(raid)
    await update_raid_messages(interaction.channel, raid)
    await interaction.followup.send(f"Locked **{raid_title(raid)}**.", ephemeral=True)


@tree.command(name="unlockroster", description="Unlock a roster so players can change roles again")
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
        allowed_mentions=discord.AllowedMentions(roles=True, users=True),
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

    await interaction.followup.send(f"```text\n{roster_export_text(raid)}\n```", ephemeral=True)


@tree.command(name="rolerestrictions", description="Turn Discord role checks on or off for a roster")
@app_commands.describe(
    enabled="True requires Tank, Healer, or DPS Discord roles to pick matching roles",
    signup_message_id="Optional signup message ID. Leave blank for newest signup in this channel.",
)
async def rolerestrictions(
    interaction: discord.Interaction,
    enabled: bool,
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

    raid["role_restrictions"] = enabled
    await save_raid_state(raid)
    await update_raid_messages(interaction.channel, raid)
    state = "enabled" if enabled else "disabled"
    await interaction.followup.send(f"Role restrictions {state} for **{raid_title(raid)}**.", ephemeral=True)


@tree.command(name="raidrole", description="Set the Discord role assigned to signed players and pinged by reminders")
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

    raid["raid_role_id"] = role.id
    raid["raid_role_name"] = role.name

    warnings = []
    for player in raid["signups"].values():
        member = interaction.guild.get_member(int(player["user_id"])) if player.get("user_id") else None
        if member is None and player.get("user_id"):
            try:
                member = await interaction.guild.fetch_member(int(player["user_id"]))
            except (discord.Forbidden, discord.NotFound):
                member = None

        if member:
            warning = await add_raid_role(member, raid)
            if warning and warning not in warnings:
                warnings.append(warning)

    await save_raid_state(raid)
    await update_raid_messages(interaction.channel, raid)

    message = f"Raid role set to {role.mention} for **{raid_title(raid)}**."
    if warnings:
        message += "\n" + "\n".join(warnings)

    await interaction.followup.send(message, ephemeral=True)


@tree.command(name="bumproster", description="Move the roster post to the bottom of the channel")
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

    old_roster_message_id = raid.get("roster_message_id")
    if old_roster_message_id:
        try:
            old_roster_message = await interaction.channel.fetch_message(old_roster_message_id)
            await old_roster_message.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            await interaction.followup.send(
                "I could not delete the old roster post. Make sure I can manage messages in this channel.",
                ephemeral=True,
            )
            return

    new_roster_message = await interaction.channel.send(embed=roster_embed(raid))
    raid["roster_message_id"] = new_roster_message.id
    await save_raid_state(raid)
    await interaction.followup.send(f"Bumped the roster for **{raid_title(raid)}**.", ephemeral=True)


@tree.command(name="clearroster", description="Clear a raid roster and remove assigned raid roles")
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

    for player in raid["signups"].values():
        await remove_raid_role_from_player(interaction.guild, raid, player)

    raid["signups"] = {}
    raid["waitlist"] = {}
    raid["standby"] = {}
    raid["confirmations"] = []
    raid["locked"] = False
    raid["reminders_sent"]["role_cleanup"] = True
    await save_raid_state(raid)
    await update_raid_messages(interaction.channel, raid)
    await interaction.followup.send(f"Cleared **{raid_title(raid)}**.", ephemeral=True)


@tree.command(name="deleteroster", description="Clear a roster, remove raid roles, and delete bot posts")
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

    for player in raid["signups"].values():
        await remove_raid_role_from_player(interaction.guild, raid, player)

    for message_id in (raid["signup_message_id"], raid.get("roster_message_id")):
        if not message_id:
            continue
        try:
            message = await interaction.channel.fetch_message(message_id)
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

    await delete_raid_row(raid["signup_message_id"])
    await interaction.followup.send(f"Deleted roster for **{raid_title(raid)}**.", ephemeral=True)


@tree.command(name="deleteraid", description="Delete a raid signup, roster post, and database entry")
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

    for player in raid["signups"].values():
        await remove_raid_role_from_player(interaction.guild, raid, player)

    for message_id in (raid["signup_message_id"], raid.get("roster_message_id")):
        if not message_id:
            continue
        try:
            message = await interaction.channel.fetch_message(message_id)
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

    await delete_raid_row(raid["signup_message_id"])
    await interaction.followup.send(f"Deleted **{raid_title(raid)}**.", ephemeral=True)


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
            "Creates a raid signup with a custom message in the signup embed and sends a separate announcement "
            f"that pings `{DEFAULT_NOTIFY_ROLE_NAME}` by default. This ping role is only notified; players still "
            f"only receive `{DEFAULT_RAID_ROLE_NAME}` when they pick a roster role."
        ),
        inline=False,
    )
    embed.add_field(
        name="/raidsignup",
        value=(
            "Creates a custom raid signup and a live roster post.\n"
            "`raid_name` is the raid name. `date` accepts `5/25`, `May 25`, or `2026-05-25`. `time` accepts `20:00`, "
            "`8:00 PM`, `8PM`, or `8 PM`. Times default to Pacific Time. `group` is optional for Group A/B style coordination."
        ),
        inline=False,
    )
    embed.add_field(
        name="/raidpreset",
        value=(
            "Creates the same signup using a preset raid name, such as Everkeep EX, M4S, or FRU. "
            "Use this when you do not want to type the full raid name."
        ),
        inline=False,
    )
    embed.add_field(
        name="Buttons",
        value=(
            "Role buttons claim open roles. Filled roles are locked. `Standby` lets unsigned players "
            "mark Tank, DPS, Healer, or all three as Any. `Resign` removes your role and reopens the spot. "
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
            "`/deleteroster` removes assigned raid roles, deletes the signup and roster posts, and removes the saved roster. "
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
        name="/rolerestrictions",
        value=(
            "Turns role checks on or off. When enabled, members need Discord roles named `Tank`, "
            "`Healer`, or `DPS` to select matching raid roles. Requires Manage Messages."
        ),
        inline=False,
    )
    embed.add_field(
        name="Reminders",
        value=(
            "The bot sends one automatic reminder only. It chooses the longest available reminder window: "
            "24 hours, otherwise 12 hours, otherwise 8 hours, otherwise 6 hours before raid time. "
            "The reminder pings the configured raid role. Two hours after raid start, the bot removes "
            "that role from signed players. The bot must be running for reminders and cleanup."
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
    raids = await fetch_scheduled_raids()

    for raid in raids:
        scheduled_at = raid.get("scheduled_at")
        if not scheduled_at:
            continue

        channel = await fetch_raid_channel(raid)

        if now >= scheduled_at + (2 * 60 * 60) and not raid["reminders_sent"].get("post_raid_delete"):
            if channel is not None:
                guild = getattr(channel, "guild", None)
                for player in raid["signups"].values():
                    await remove_raid_role_from_player(guild, raid, player)

                if await delete_raid_messages(channel, raid):
                    await delete_raid_row(raid["signup_message_id"])
                else:
                    print(f"Post-raid cleanup could not delete messages for {raid['signup_message_id']}")
            else:
                print(f"Post-raid cleanup could not access channel for {raid['signup_message_id']}")
            continue

        if now > scheduled_at:
            continue

        reminder_offset = raid.get("reminder_offset")
        if reminder_offset is None:
            reminder_offset = choose_reminder_offset(scheduled_at)
            raid["reminder_offset"] = reminder_offset
            await save_raid_state(raid)

        if not reminder_offset or raid["reminders_sent"].get("automatic"):
            continue

        if now < scheduled_at - reminder_offset or now > scheduled_at:
            continue

        if channel is None:
            continue

        try:
            ping_text = reminder_ping_text(channel, raid)
            await channel.send(
                f"Reminder: **{raid_title(raid)}** starts in {reminder_label(reminder_offset)}.\n"
                f"{schedule_text(raid)}\n"
                f"Ping: {ping_text}",
                allowed_mentions=discord.AllowedMentions(roles=True, users=True),
            )
        except discord.Forbidden:
            continue

        raid["reminders_sent"]["automatic"] = True
        await save_raid_state(raid)


async def reminder_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await send_due_reminders()
        except Exception as exc:
            print(f"Reminder check failed: {exc}")
        await asyncio.sleep(60)


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
                       notify_role_id, notify_role_name
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

    print(f"Bot ready! Logged in as {bot.user}")


if __name__ == "__main__":
    bot.run(os.getenv("DISCORD_TOKEN"))
