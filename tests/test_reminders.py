import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

import bot
from test_storage import make_raid


def player(user_id, roles=None):
    result = {
        "user_id": user_id, "display_name": "Player", "mention": f"<@{user_id}>",
        "job": None, "needs_mount": None, "ready_status": None,
    }
    if roles is not None:
        result["roles"] = roles
    return result


class ReminderMessage:
    def __init__(self, message_id, *, error=None):
        self.id = message_id
        self.error = error
        self.deleted = False
        self.delete_attempts = 0

    async def delete(self):
        self.delete_attempts += 1
        if self.error:
            raise self.error
        self.deleted = True


class ReminderChannel:
    def __init__(self):
        self.guild = SimpleNamespace(id=1)
        self.messages = {}
        self.sent = []

    def get_partial_message(self, message_id):
        return self.messages.setdefault(message_id, ReminderMessage(message_id))

    async def send(self, content=None, **kwargs):
        message = ReminderMessage(1000 + len(self.sent))
        self.messages[message.id] = message
        self.sent.append((content, kwargs, message))
        return message


class StartReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db_path = bot.DB_PATH
        self.directory = tempfile.TemporaryDirectory()
        bot.DB_PATH = str(Path(self.directory.name) / "raids.db")
        await bot.init_db()
        self.channel = ReminderChannel()
        self.now = 1_000_000

    async def asyncTearDown(self):
        bot.DB_PATH = self.original_db_path
        self.directory.cleanup()

    def raid(self, *, scheduled_at=None):
        return make_raid(scheduled_at=self.now + 300 if scheduled_at is None else scheduled_at) | {
            "guild_id": 1, "start_reminder_message_ids": [],
        }

    def fill_except(self, raid, open_slots):
        raid["signups"] = {
            role_key: player(101 + index)
            for index, (role_key, *_) in enumerate(bot.ROLES)
            if role_key not in open_slots
        }

    async def process(self, at):
        with (
            patch.object(bot, "datetime") as clock,
            patch.object(bot, "fetch_raid_channel", new=AsyncMock(return_value=self.channel)),
        ):
            clock.now.return_value = datetime.fromtimestamp(at, timezone.utc)
            await bot.process_due_raid(123)

    def test_full_roster_suppresses_standby_recruitment_pings(self):
        raid = self.raid()
        self.fill_except(raid, set())
        raid["standby"] = {"42": player(42, ["Tank", "Melee DPS"])}
        text = bot.five_minute_reminder_text(raid, now=self.now)
        self.assertNotIn("<@42>", text)
        self.assertNotIn(":R>", text)
        allowed = bot.allowed_mentions_for_start(raid)
        self.assertNotIn(42, [user.id for user in allowed.users])
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.roles)

    def test_open_tank_slot_pings_matching_multiselect_player_only_once(self):
        raid = self.raid()
        self.fill_except(raid, {"MT"})
        raid["standby"] = {
            "42": player(42, ["Tank", "Melee DPS"]),
            "43": player(43, ["Healer"]),
            "44": player(44, ["Range DPS"]),
            "102": player(102, ["Tank"]),
        }
        text = bot.five_minute_reminder_text(raid, now=self.now)
        self.assertEqual(text.count("<@42>"), 1)
        self.assertNotIn("<@43>", text)
        self.assertNotIn("<@44>", text)
        allowed_ids = [user.id for user in bot.allowed_mentions_for_start(raid).users]
        self.assertEqual(allowed_ids.count(42), 1)
        self.assertNotIn(43, allowed_ids)
        self.assertNotIn(44, allowed_ids)

    def test_both_ranged_vacancies_match_range_dps_without_duplicate_pings(self):
        raid = self.raid()
        self.fill_except(raid, {"R1", "R2"})
        raid["standby"] = {
            "42": player(42, ["Range DPS"]),
            "43": player(43, ["Melee DPS"]),
        }
        text = bot.five_minute_reminder_text(raid, now=self.now)
        self.assertEqual(text.count("<@42>"), 1)
        self.assertNotIn("<@43>", text)

    async def test_five_minute_send_saves_message_ids_for_start_cleanup(self):
        raid = self.raid()
        raid["standby"] = {"42": player(42, ["Tank", "Melee DPS"])}
        await bot.insert_raid(raid)
        await self.process(self.now)
        saved = await bot.fetch_signup(123)
        self.assertEqual(saved["start_reminder_message_ids"], [1000])
        self.assertTrue(saved["reminders_sent"]["five_minute"])
        self.assertEqual(self.channel.sent[0][0].count("<@42>"), 1)
        self.assertNotIn("No one is signed up yet", self.channel.sent[0][0])

    async def test_start_deletes_saved_final_reminder_without_reposting_or_removing_roles(self):
        raid = self.raid(scheduled_at=self.now)
        raid["start_reminder_message_ids"] = [1000]
        raid["reminders_sent"] = {"five_minute": True}
        await bot.insert_raid(raid)
        with patch.object(bot, "remove_all_raid_roles", new=AsyncMock()) as remove:
            await self.process(self.now)
        self.assertTrue(self.channel.messages[1000].deleted)
        self.assertEqual(self.channel.sent, [])
        self.assertEqual((await bot.fetch_signup(123))["start_reminder_message_ids"], [])
        remove.assert_not_awaited()

    async def test_start_reminder_cleanup_treats_404_as_done_but_retries_403(self):
        raid = self.raid()
        raid["start_reminder_message_ids"] = [1000, 1001]
        await bot.insert_raid(raid)
        self.channel.messages[1000] = ReminderMessage(
            1000, error=bot.discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Gone"),
        )
        self.channel.messages[1001] = ReminderMessage(
            1001, error=bot.discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Denied"),
        )
        with self.assertLogs("rosterbot", level="ERROR"):
            self.assertFalse(await bot.delete_start_reminders(self.channel, raid))
        self.assertEqual((await bot.fetch_signup(123))["start_reminder_message_ids"], [1001])
        self.channel.messages[1001].error = None
        self.assertTrue(await bot.delete_start_reminders(self.channel, raid))
        self.assertEqual((await bot.fetch_signup(123))["start_reminder_message_ids"], [])

    async def test_restart_after_start_removes_old_message_without_a_duplicate_ping(self):
        raid = self.raid(scheduled_at=self.now - 90)
        raid["start_reminder_message_ids"] = [1000]
        raid["reminders_sent"] = {"five_minute": True}
        await bot.insert_raid(raid)
        await bot.init_db()
        await self.process(self.now)
        self.assertTrue(self.channel.messages[1000].deleted)
        self.assertEqual(self.channel.sent, [])
        self.assertIsNotNone(await bot.fetch_signup(123))

    async def test_legacy_offer_posts_migrate_to_valid_deduplicated_start_cleanup_ids(self):
        raid = self.raid(scheduled_at=self.now)
        raid["start_reminder_message_ids"] = [1000, 1000, False, "1001", -1]
        await bot.insert_raid(raid)
        async with aiosqlite.connect(bot.DB_PATH) as db:
            async with db.execute("PRAGMA table_info(signups)") as cursor:
                self.assertNotIn("standby_offers", {row[1] for row in await cursor.fetchall()})
            await db.execute("ALTER TABLE signups ADD COLUMN standby_offers TEXT NOT NULL DEFAULT '{}'")
            await db.execute(
                "UPDATE signups SET standby_offers = ? WHERE signup_message_id = 123",
                ('{"MT":{"message_id":1000},"OT":{"message_id":1001},'
                 '"H1":{"message_id":true},"H2":{"message_id":-2},'
                 '"M1":{"message_id":"1002"},"M2":[],"R1":7,'
                 '"R2":{"message_id":9223372036854775808}}',),
            )
            await db.commit()
        await bot.init_db()
        await bot.init_db()
        self.assertEqual((await bot.fetch_signup(123))["start_reminder_message_ids"], [1000, 1001])
        async with aiosqlite.connect(bot.DB_PATH) as db:
            async with db.execute("SELECT standby_offers FROM signups WHERE signup_message_id = 123") as cursor:
                self.assertEqual((await cursor.fetchone())[0], "{}")
        await self.process(self.now)
        self.assertTrue(self.channel.messages[1000].deleted)
        self.assertTrue(self.channel.messages[1001].deleted)

    async def test_two_hour_boundary_deletes_posts_then_cleans_roles(self):
        raid = self.raid(scheduled_at=self.now)
        raid["signups"] = {"MT": player(42)}
        raid["announcement_message_id"] = 456
        await bot.insert_raid(raid)
        await self.process(self.now + 7199)
        self.assertEqual(self.channel.messages, {})
        self.assertIsNotNone(await bot.fetch_signup(123))

        async def clean_roles(guild, snapshot):
            saved = await bot.fetch_signup(123)
            self.assertTrue(saved["cleanup_pending"])
            self.assertEqual(saved["signups"]["MT"]["user_id"], 42)
            self.assertTrue(self.channel.messages[123].deleted)
            self.assertTrue(self.channel.messages[456].deleted)
            return []

        with patch.object(bot, "remove_all_raid_roles", new=AsyncMock(side_effect=clean_roles)):
            await self.process(self.now + 7200)
        self.assertIsNone(await bot.fetch_signup(123))

    async def test_failed_roles_after_two_hour_post_deletion_remain_durable(self):
        raid = self.raid(scheduled_at=self.now)
        raid["signups"] = {"MT": player(42)}
        await bot.insert_raid(raid)
        with (
            patch.object(bot, "remove_all_raid_roles", new=AsyncMock(return_value=["Saved role failure"])),
            self.assertLogs("rosterbot", level="WARNING"),
        ):
            await self.process(self.now + 7200)
        saved = await bot.fetch_signup(123)
        self.assertTrue(saved["cleanup_pending"])
        self.assertEqual(saved["signups"]["MT"]["user_id"], 42)
        self.assertTrue(self.channel.messages[123].deleted)
        await bot.init_db()
        with patch.object(bot, "remove_all_raid_roles", new=AsyncMock(return_value=[])):
            await self.process(self.now + 7260)
        self.assertIsNone(await bot.fetch_signup(123))

    async def test_explicit_deletion_also_removes_tracked_final_reminder(self):
        raid = self.raid()
        raid["start_reminder_message_ids"] = [1000]
        await bot.insert_raid(raid)
        event = SimpleNamespace(channel=self.channel, guild=self.channel.guild)
        deleted, warnings = await bot.delete_raid_for_command(event, raid)
        self.assertTrue(deleted)
        self.assertEqual(warnings, [])
        self.assertTrue(self.channel.messages[1000].deleted)
        self.assertTrue(self.channel.messages[123].deleted)
        self.assertIsNone(await bot.fetch_signup(123))

    async def test_large_matching_standby_list_is_split_without_dropped_pings(self):
        raid = self.raid()
        self.fill_except(raid, {"MT"})
        ids = list(range(10000, 10125))
        raid["standby"] = {str(user_id): player(user_id, ["Tank"]) for user_id in ids}
        await bot.insert_raid(raid)
        await self.process(self.now)
        self.assertGreater(len(self.channel.sent), 1)
        text = "\n".join(content for content, _, _ in self.channel.sent)
        for user_id in ids:
            self.assertEqual(text.count(f"<@{user_id}>"), 1)
        for signed in raid["signups"].values():
            self.assertEqual(text.count(f"<@{signed['user_id']}>"), 1)
        for content, kwargs, _ in self.channel.sent:
            self.assertLessEqual(len(content), 2000)
            self.assertLessEqual(len(kwargs["allowed_mentions"].users), 100)
            self.assertFalse(kwargs["allowed_mentions"].everyone)
            self.assertFalse(kwargs["allowed_mentions"].roles)
        saved = await bot.fetch_signup(123)
        self.assertEqual(saved["start_reminder_message_ids"], [message.id for _, _, message in self.channel.sent])

    async def test_partial_batch_failure_rolls_back_posts_and_retries_all_matching_players(self):
        raid = self.raid()
        ids = list(range(10000, 10125))
        raid["standby"] = {str(user_id): player(user_id, ["Tank"]) for user_id in ids}
        await bot.insert_raid(raid)
        original_send = self.channel.send
        attempts = 0

        async def fail_second_send(content=None, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise bot.discord.HTTPException(SimpleNamespace(status=500, reason="Unavailable"), "Retry")
            return await original_send(content, **kwargs)

        with (
            patch.object(self.channel, "send", new=fail_second_send),
            self.assertLogs("rosterbot", level="ERROR"),
        ):
            await self.process(self.now)
        saved = await bot.fetch_signup(123)
        self.assertFalse(saved["reminders_sent"].get("five_minute"))
        self.assertEqual(saved["start_reminder_message_ids"], [])
        self.assertTrue(self.channel.messages[1000].deleted)

        await self.process(self.now + 60)
        saved = await bot.fetch_signup(123)
        self.assertTrue(saved["reminders_sent"]["five_minute"])
        self.assertEqual(saved["start_reminder_message_ids"], [1001, 1002])
        delivered = "\n".join(content for content, _, _ in self.channel.sent[1:])
        for user_id in ids:
            self.assertEqual(delivered.count(f"<@{user_id}>"), 1)

    async def test_failed_partial_rollback_is_saved_and_blocks_reposting_until_removed(self):
        raid = self.raid()
        raid["standby"] = {str(user_id): player(user_id, ["Tank"]) for user_id in range(10000, 10125)}
        await bot.insert_raid(raid)
        original_send = self.channel.send
        attempts = 0

        async def fail_second_send_and_rollback(content=None, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise bot.discord.HTTPException(SimpleNamespace(status=500, reason="Unavailable"), "Retry")
            message = await original_send(content, **kwargs)
            message.error = bot.discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Denied")
            return message

        with (
            patch.object(self.channel, "send", new=fail_second_send_and_rollback),
            self.assertLogs("rosterbot", level="ERROR"),
        ):
            await self.process(self.now)
        self.assertEqual((await bot.fetch_signup(123))["start_reminder_message_ids"], [1000])
        with self.assertLogs("rosterbot", level="ERROR"):
            await self.process(self.now + 60)
        self.assertEqual(len(self.channel.sent), 1)
        self.assertFalse((await bot.fetch_signup(123))["reminders_sent"].get("five_minute"))

        self.channel.messages[1000].error = None
        await self.process(self.now + 120)
        saved = await bot.fetch_signup(123)
        self.assertTrue(saved["reminders_sent"]["five_minute"])
        self.assertEqual(saved["start_reminder_message_ids"], [1001, 1002])
        self.assertTrue(self.channel.messages[1000].deleted)

    async def test_slow_send_crossing_start_is_deleted_immediately_after_save(self):
        raid = self.raid(scheduled_at=self.now + 1)
        await bot.insert_raid(raid)
        original_send = self.channel.send
        with (
            patch.object(bot, "datetime") as clock,
            patch.object(bot, "fetch_raid_channel", new=AsyncMock(return_value=self.channel)),
        ):
            clock.now.return_value = datetime.fromtimestamp(self.now, timezone.utc)

            async def delayed_send(content=None, **kwargs):
                message = await original_send(content, **kwargs)
                clock.now.return_value = datetime.fromtimestamp(self.now + 2, timezone.utc)
                return message

            with patch.object(self.channel, "send", new=delayed_send):
                await bot.process_due_raid(123)
        self.assertTrue(self.channel.messages[1000].deleted)
        self.assertEqual((await bot.fetch_signup(123))["start_reminder_message_ids"], [])

    async def test_scheduler_returns_the_upcoming_start_for_precise_cleanup_wakeup(self):
        raid = self.raid(scheduled_at=self.now + 12)
        await bot.insert_raid(raid)
        with (
            patch.object(bot, "datetime") as clock,
            patch.object(bot, "fetch_raid_channel", new=AsyncMock(return_value=self.channel)),
        ):
            clock.now.return_value = datetime.fromtimestamp(self.now, timezone.utc)
            next_start = await bot.send_due_reminders()
        self.assertEqual(next_start, self.now + 12)

    async def test_loop_wakes_at_start_instead_of_waiting_a_full_poll_interval(self):
        with (
            patch.object(bot.bot, "wait_until_ready", new=AsyncMock()),
            patch.object(bot.bot, "is_closed", side_effect=[False, True]),
            patch.object(bot, "send_due_reminders", new=AsyncMock(return_value=self.now + 10)),
            patch.object(bot, "datetime") as clock,
            patch.object(bot.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            clock.now.return_value = datetime.fromtimestamp(self.now + 0.25, timezone.utc)
            await bot.reminder_loop()
        sleep.assert_awaited_once_with(9.75)

    async def test_blocked_role_recovery_worker_does_not_delay_start_message_deletion(self):
        raid = self.raid(scheduled_at=self.now)
        raid["start_reminder_message_ids"] = [1000]
        raid["reminders_sent"] = {"five_minute": True}
        await bot.insert_raid(raid)
        recovery_started = asyncio.Event()
        release_recovery = asyncio.Event()

        async def blocked_recovery():
            recovery_started.set()
            await release_recovery.wait()
            return []

        with (
            patch.object(bot.bot, "wait_until_ready", new=AsyncMock()),
            patch.object(bot.bot, "is_closed", return_value=False),
            patch.object(bot, "retry_pending_role_updates", new=blocked_recovery),
            patch.object(bot, "datetime") as clock,
            patch.object(bot, "fetch_raid_channel", new=AsyncMock(return_value=self.channel)),
        ):
            clock.now.return_value = datetime.fromtimestamp(self.now, timezone.utc)
            task = asyncio.create_task(bot.role_recovery_loop())
            try:
                await asyncio.wait_for(recovery_started.wait(), timeout=1)
                await asyncio.wait_for(bot.send_due_reminders(), timeout=1)
                self.assertFalse(task.done())
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(self.channel.messages[1000].deleted)
        self.assertEqual((await bot.fetch_signup(123))["start_reminder_message_ids"], [])
