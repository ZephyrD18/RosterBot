import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite

import bot


class FakeMessage:
    def __init__(self, message_id: int, content=None, embed=None):
        self.id = message_id
        self.content = content
        self.embed = embed
        self.jump_url = f"https://discord.test/channels/1/2/{message_id}"
        self.view = None
        self.deleted = False

    async def edit(self, *, embed=None, view=None):
        self.embed = embed
        self.view = view

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self):
        self.id = 789
        self.guild = FakeGuild()
        self.messages = []

    async def send(self, content=None, *, embed=None, **kwargs):
        message = FakeMessage(len(self.messages) + 1, content=content, embed=embed)
        self.messages.append(message)
        return message


class FakeGuild:
    roles = []


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, **kwargs):
        self.messages.append(content)


class FakeResponse:
    def __init__(self):
        self.embed = None
        self.deferred = False

    async def defer(self, **kwargs):
        self.deferred = True

    def is_done(self):
        return self.deferred

    async def send_message(self, *, embed=None, **kwargs):
        self.embed = embed


class FakeInteraction:
    def __init__(self):
        self.channel = FakeChannel()
        self.guild = self.channel.guild
        self.channel_id = self.channel.id
        self.followup = FakeFollowup()
        self.response = FakeResponse()
        self.user = type("FakeUser", (), {"id": 42})()


def sample_raid() -> dict:
    return {
        "signup_message_id": 123,
        "roster_message_id": 456,
        "channel_id": 789,
        "raid_name": "Recollection EX",
        "date": "9/12/2026",
        "time": "10PM",
        "signups": {},
        "standby": {},
        "scheduled_at": 1789275600,
        "timezone": "PT",
        "locked": False,
        "group_name": None,
        "reminders_sent": {},
        "reminder_offset": 86400,
        "raid_role_id": None,
        "raid_role_name": bot.DEFAULT_RAID_ROLE_NAME,
        "announcement_message_id": None,
        "setup_message": "This belongs only in the announcement.",
        "notify_role_id": None,
        "notify_role_name": None,
        "meeting_server": "Seraph",
    }


class RosterFormattingTests(unittest.TestCase):
    def test_removed_role_restriction_command_is_not_registered(self):
        command_names = {command.name for command in bot.tree.get_commands()}

        self.assertNotIn("rolerestrictions", command_names)

    def test_standby_removal_reports_whether_an_entry_existed(self):
        standby = {"42": {"roles": ["Tank"]}}

        self.assertTrue(bot.remove_user_from_standby(standby, 42))
        self.assertFalse(bot.remove_user_from_standby(standby, 42))
        self.assertEqual(standby, {})

    def test_signup_embed_does_not_repeat_announcement_message(self):
        embed = bot.signup_embed(sample_raid())

        self.assertNotIn("This belongs only in the announcement.", embed.description)
        self.assertIn("Choose your light party role below.", embed.description)

    def test_filled_open_slot_is_struck_through(self):
        raid = sample_raid()
        raid["signups"]["MT"] = {
            "user_id": 42,
            "display_name": "Tester",
            "mention": "<@42>",
            "needs_mount": True,
        }

        embed = bot.roster_embed(raid)
        open_slots = next(field.value for field in embed.fields if "Open Slots" in field.name)

        self.assertIn("~~Main Tank~~, Off Tank", open_slots)

    def test_large_standby_list_stays_within_discord_field_limit(self):
        lines = [f"<@{user_id}> - Any - Needs Mount" for user_id in range(1000, 1100)]

        value = bot.fit_embed_lines(lines, "No standby players.")

        self.assertLessEqual(len(value), 1024)
        self.assertIn("more.", value)

    def test_large_roster_export_is_split_into_discord_sized_chunks(self):
        text = "\n".join(f"Standby player {index}" for index in range(300))

        chunks = bot.chunk_text_lines(text)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1900 for chunk in chunks))
        self.assertEqual("\n".join(chunks), text)

    def test_compact_pm_time_uses_pacific_daylight_time(self):
        timestamp = bot.parse_raid_datetime("9/12/2026", "10PM", "PT")
        expected = int(datetime(2026, 9, 13, 5, 0, tzinfo=timezone.utc).timestamp())

        self.assertEqual(timestamp, expected)

    def test_reminder_window_allows_normal_command_latency(self):
        now = 1_000_000

        self.assertEqual(bot.choose_reminder_offset(now + 86400 - 30, now=now), 86400)


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db_path = bot.DB_PATH
        self.temp_directory = tempfile.TemporaryDirectory()
        bot.DB_PATH = str(Path(self.temp_directory.name) / "raids.db")
        bot.raid_locks.clear()
        await bot.init_db()

    async def asyncTearDown(self):
        bot.raid_locks.clear()
        bot.DB_PATH = self.original_db_path
        self.temp_directory.cleanup()

    async def test_reminder_update_does_not_overwrite_roster_data(self):
        signups = {"MT": {"user_id": 42, "display_name": "Tester", "mention": "<@42>"}}
        async with aiosqlite.connect(bot.DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO signups (signup_message_id, raid_name, date, time, signups)
                VALUES (?, ?, ?, ?, ?)
                """,
                (123, "Recollection EX", "9/12/2026", "10PM", json.dumps(signups)),
            )
            await db.commit()

        loaded_raid = await bot.fetch_signup(123)
        self.assertEqual(loaded_raid["signups"]["MT"]["user_id"], 42)
        self.assertNotIn("waitlist", loaded_raid)
        self.assertNotIn("confirmations", loaded_raid)
        self.assertNotIn("role_restrictions", loaded_raid)
        await bot.save_raid_state(loaded_raid)
        expected_signups = loaded_raid["signups"]

        raid = sample_raid()
        raid["reminders_sent"] = {"automatic": True}
        raid["reminder_offset"] = 43200
        await bot.save_reminder_state(raid)

        async with aiosqlite.connect(bot.DB_PATH) as db:
            async with db.execute(
                "SELECT signups, reminders_sent, reminder_offset FROM signups WHERE signup_message_id = ?",
                (123,),
            ) as cursor:
                row = await cursor.fetchone()

        self.assertEqual(json.loads(row[0]), expected_signups)
        self.assertEqual(json.loads(row[1]), {"automatic": True})
        self.assertEqual(row[2], 43200)

    async def test_create_raid_saves_all_posts_and_keeps_roster_last(self):
        interaction = FakeInteraction()

        await bot.create_raid(
            interaction,
            "Recollection EX",
            "12/31/2099",
            "10PM",
            setup_message="Pick a role and get ready!",
            send_announcement=True,
            meeting_server="Seraph",
        )

        self.assertEqual(len(interaction.channel.messages), 3)
        self.assertIsNotNone(interaction.channel.messages[0].view)
        self.assertIn("Pick a role and get ready!", interaction.channel.messages[1].content)

        raid = await bot.fetch_signup(interaction.channel.messages[0].id)
        self.assertEqual(raid["announcement_message_id"], interaction.channel.messages[1].id)
        self.assertEqual(raid["roster_message_id"], interaction.channel.messages[2].id)

    async def test_help_embed_stays_within_discord_limits(self):
        interaction = FakeInteraction()

        await bot.help_command.callback(interaction)
        embed = interaction.response.embed

        self.assertLessEqual(len(embed), 6000)
        self.assertLessEqual(len(embed.fields), 25)
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))

    async def test_resign_removes_a_standby_only_player(self):
        raid = sample_raid()
        raid["standby"] = {
            "42": {
                "user_id": 42,
                "display_name": "Tester",
                "mention": "<@42>",
                "roles": ["Tank", "DPS"],
                "needs_mount": True,
            }
        }

        async with aiosqlite.connect(bot.DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO signups (
                    signup_message_id, roster_message_id, channel_id, raid_name, date, time,
                    signups, standby, scheduled_at, timezone, locked, group_name, reminders_sent,
                    reminder_offset, raid_role_id, raid_role_name, announcement_message_id,
                    setup_message, notify_role_id, notify_role_name, meeting_server
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
                    json.dumps(raid["signups"]),
                    json.dumps(raid["standby"]),
                    raid["scheduled_at"],
                    raid["timezone"],
                    int(raid["locked"]),
                    raid["group_name"],
                    json.dumps(raid["reminders_sent"]),
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

        interaction = FakeInteraction()
        view = bot.RaidSignupView(raid)
        with patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True)):
            await view.resign_callback(interaction)

        saved_raid = await bot.fetch_signup(raid["signup_message_id"])
        self.assertEqual(saved_raid["standby"], {})
        self.assertIn("resigned from the standby list", interaction.followup.messages[-1])


if __name__ == "__main__":
    unittest.main()
