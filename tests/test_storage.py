import asyncio
import gc
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

import bot


def make_raid(message_id=123, scheduled_at=1_000_000):
    return {
        "signup_message_id": message_id,
        "roster_message_id": None,
        "channel_id": 789,
        "raid_name": "Test raid",
        "date": "1/1/2030",
        "time": "8PM",
        "signups": {},
        "standby": {},
        "scheduled_at": scheduled_at,
        "timezone": "PT",
        "locked": False,
        "group_name": None,
        "reminders_sent": {},
        "reminder_offset": 86400,
        "raid_role_id": None,
        "raid_role_name": bot.DEFAULT_RAID_ROLE_NAME,
        "announcement_message_id": None,
        "setup_message": None,
        "notify_role_id": None,
        "notify_role_name": None,
        "meeting_server": None,
    }


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db_path = bot.DB_PATH
        self.directory = tempfile.TemporaryDirectory()
        bot.DB_PATH = str(Path(self.directory.name) / "raids.db")
        await bot.init_db()

    async def asyncTearDown(self):
        bot.DB_PATH = self.original_db_path
        self.directory.cleanup()

    async def run_reminders(self, now):
        with patch.object(bot, "datetime") as clock:
            clock.now.return_value = datetime.fromtimestamp(now, timezone.utc)
            await bot.send_due_reminders()

    async def test_fresh_database_omits_removed_features_and_indexes_channel_lookup(self):
        async with aiosqlite.connect(bot.DB_PATH) as db:
            async with db.execute("PRAGMA table_info(signups)") as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            async with db.execute(
                "EXPLAIN QUERY PLAN SELECT signup_message_id FROM signups "
                "WHERE channel_id = ? ORDER BY signup_message_id DESC LIMIT 1",
                (789,),
            ) as cursor:
                plan = await cursor.fetchall()
            async with db.execute(
                "EXPLAIN QUERY PLAN SELECT signup_message_id FROM signups WHERE scheduled_at <= ?",
                (1_000_000,),
            ) as cursor:
                scheduler_plan = await cursor.fetchall()

        self.assertTrue({"waitlist", "confirmations", "role_restrictions"}.isdisjoint(columns))
        self.assertTrue(any("signups_channel_latest" in row[3] for row in plan))
        self.assertTrue(any("signups_scheduled" in row[3] for row in scheduler_plan))

    async def test_legacy_migration_preserves_saved_and_retired_data(self):
        bot.DB_PATH = str(Path(self.directory.name) / "legacy.db")
        async with aiosqlite.connect(bot.DB_PATH) as db:
            await db.execute(
                "CREATE TABLE signups (message_id INTEGER PRIMARY KEY, raid_name TEXT, "
                "date TEXT, time TEXT, signups TEXT, role_restrictions INTEGER, waitlist TEXT)"
            )
            await db.execute(
                "INSERT INTO signups VALUES (123, 'Old raid', '1/1/2030', '8PM', '{}', 1, 'saved legacy data')"
            )
            await db.commit()

        await bot.init_db()
        await bot.init_db()
        raid = await bot.fetch_signup(123)
        self.assertEqual(raid["raid_name"], "Old raid")
        self.assertEqual(raid["standby"], {})
        async with aiosqlite.connect(bot.DB_PATH) as db:
            async with db.execute("SELECT role_restrictions, waitlist FROM signups") as cursor:
                self.assertEqual(await cursor.fetchone(), (1, "saved legacy data"))

    async def test_latest_signup_is_scoped_to_channel(self):
        for message_id in (123, 124, 125):
            raid = make_raid(message_id)
            if message_id == 125:
                raid["channel_id"] = 999
            await bot.insert_raid(raid)

        self.assertEqual((await bot.fetch_latest_signup_for_channel(789))["signup_message_id"], 124)
        self.assertIsNone(await bot.fetch_latest_signup_for_channel(1000))

    async def test_shared_role_candidates_include_legacy_empty_default_names(self):
        for message_id, name in ((123, None), (124, ""), (125, "Different role")):
            raid = make_raid(message_id)
            raid["raid_role_name"] = name
            await bot.insert_raid(raid)

        candidates = await bot.fetch_raids_for_role(10, bot.DEFAULT_RAID_ROLE_NAME)
        self.assertEqual({raid["signup_message_id"] for raid in candidates}, {123, 124})

    async def test_distinct_roles_with_same_name_do_not_retain_the_removed_role(self):
        role = SimpleNamespace(id=10, name="Raid")
        other_role = SimpleNamespace(id=11, name="Raid")
        guild = SimpleNamespace(id=1, roles=[role, other_role])
        guild.get_role = lambda role_id: next((role for role in guild.roles if role.id == role_id), None)
        member = SimpleNamespace(id=42, guild=guild, remove_roles=AsyncMock())
        current = make_raid()
        current.update(raid_role_id=10, raid_role_name="Raid")
        other = make_raid(124)
        other.update(raid_role_id=11, raid_role_name="Raid", signups={"MT": {"user_id": 42}})
        await bot.insert_raid(current)
        await bot.insert_raid(other)

        with patch.object(bot, "fetch_raid_channel", new=AsyncMock(return_value=SimpleNamespace(guild=guild))):
            self.assertIsNone(await bot.remove_raid_role(member, current))

        member.remove_roles.assert_awaited_once_with(role, reason="Left Test raid")

    async def test_scheduler_excludes_distant_and_unscheduled_raids(self):
        now = 1_000_000
        for message_id, timestamp in ((123, now + 90000), (124, None), (125, now + 86400)):
            await bot.insert_raid(make_raid(message_id, timestamp))

        self.assertEqual([raid["signup_message_id"] for raid in await bot.fetch_scheduled_raids(now)], [125])

    async def test_idle_scheduler_avoids_raid_reload_channel_fetch_and_database_writes(self):
        now = 1_000_000
        raid = make_raid(scheduled_at=now + 3600)
        raid["reminder_offset"] = None
        await bot.insert_raid(raid)

        with (
            patch.object(bot, "fetch_signup", new=AsyncMock()) as fetch,
            patch.object(bot, "fetch_raid_channel", new=AsyncMock()) as channel,
            patch.object(bot, "save_reminder_state", new=AsyncMock()) as save,
        ):
            await self.run_reminders(now)
            await self.run_reminders(now + 60)

        fetch.assert_not_awaited()
        channel.assert_not_awaited()
        save.assert_not_awaited()

    async def test_final_reminder_suppresses_stale_automatic_reminder(self):
        now = 1_000_000
        await bot.insert_raid(make_raid(scheduled_at=now + 240))
        channel = SimpleNamespace(guild=None, send=AsyncMock())
        with patch.object(bot, "fetch_raid_channel", new=AsyncMock(return_value=channel)):
            await self.run_reminders(now)
            await self.run_reminders(now + 60)

        self.assertEqual(channel.send.await_count, 1)
        self.assertIn(f"starts <t:{now + 240}:R>", channel.send.await_args.args[0])
        self.assertTrue((await bot.fetch_signup(123))["reminders_sent"]["five_minute"])

    async def test_delayed_automatic_reminder_uses_actual_discord_timestamp(self):
        now = 1_000_000
        await bot.insert_raid(make_raid(scheduled_at=now + 3600))
        channel = SimpleNamespace(guild=None, send=AsyncMock())
        with patch.object(bot, "fetch_raid_channel", new=AsyncMock(return_value=channel)):
            await self.run_reminders(now)

        content = channel.send.await_args.args[0]
        self.assertIn(f"starts <t:{now + 3600}:R>", content)
        self.assertNotIn("starts in 24 hours", content)

    async def test_one_raid_failure_does_not_block_other_reminders(self):
        now = 1_000_000
        for message_id in (123, 124):
            await bot.insert_raid(make_raid(message_id, now + 240))

        with (
            patch.object(bot, "process_due_raid", new=AsyncMock(side_effect=[RuntimeError("failed"), None])) as process,
            self.assertLogs("rosterbot", level="ERROR"),
        ):
            await self.run_reminders(now)

        self.assertEqual(process.await_count, 2)

    async def test_deleting_raid_stops_its_persistent_view(self):
        raid = make_raid()
        await bot.insert_raid(raid)
        view = bot.RaidSignupView(raid)
        bot.bot.add_view(view, message_id=123)
        try:
            await bot.delete_raid_row(123)
            self.assertTrue(view.is_finished())
            self.assertNotIn(view, bot.bot.persistent_views)
        finally:
            view.stop()

    async def test_cleanup_marks_database_row_pending_before_role_removal(self):
        now = 1_000_000
        await bot.insert_raid(make_raid(scheduled_at=now - 7200))
        channel = SimpleNamespace(guild=object())

        async def remove_roles(guild, raid):
            saved = await bot.fetch_signup(raid["signup_message_id"])
            self.assertTrue(saved["cleanup_pending"])
            self.assertTrue(saved["locked"])
            return []

        with (
            patch.object(bot, "fetch_raid_channel", new=AsyncMock(return_value=channel)),
            patch.object(bot, "delete_raid_messages", new=AsyncMock(return_value=True)),
            patch.object(bot, "remove_all_raid_roles", new=AsyncMock(side_effect=remove_roles)) as remove,
        ):
            await self.run_reminders(now)

        remove.assert_awaited_once()

    async def test_failed_post_cleanup_keeps_roster_and_assigned_roles(self):
        now = 1_000_000
        await bot.insert_raid(make_raid(scheduled_at=now - 7200))
        with (
            patch.object(bot, "fetch_raid_channel", new=AsyncMock(return_value=object())),
            patch.object(bot, "delete_raid_messages", new=AsyncMock(return_value=False)),
            patch.object(bot, "remove_all_raid_roles", new=AsyncMock()) as remove,
            self.assertLogs("rosterbot", level="WARNING"),
        ):
            await self.run_reminders(now)

        self.assertIsNotNone(await bot.fetch_signup(123))
        remove.assert_not_awaited()


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_restores_views_and_close_cancels_owned_scheduler(self):
        client = bot.RosterClient(intents=bot.discord.Intents.none())
        scheduler_started = asyncio.Event()

        async def scheduler():
            scheduler_started.set()
            await asyncio.Event().wait()

        with (
            patch.object(bot, "init_db", new=AsyncMock()) as initialize,
            patch.object(bot, "fetch_all_raids", new=AsyncMock(return_value=[make_raid()])),
            patch.object(bot.tree, "sync", new=AsyncMock()) as sync,
            patch.object(bot, "reminder_loop", new=scheduler),
            patch.object(bot.discord.Client, "close", new=AsyncMock()) as close,
        ):
            await client.setup_hook()
            await scheduler_started.wait()
            task = client.reminder_task
            self.assertEqual(len(client.persistent_views), 1)
            self.assertEqual(client.persistent_views[0].signup_message_id, 123)
            await client.close()

        initialize.assert_awaited_once()
        sync.assert_awaited_once()
        close.assert_awaited_once()
        self.assertTrue(task.cancelled())
        self.assertIsNone(client.reminder_task)
        for view in client.persistent_views:
            view.stop()

    async def test_ready_event_does_not_repeat_setup(self):
        with patch.object(bot, "init_db", new=AsyncMock()) as initialize:
            await bot.on_ready()
            await bot.on_ready()
        initialize.assert_not_awaited()

    async def test_discord_connection_closes_even_if_scheduler_failed(self):
        client = bot.RosterClient(intents=bot.discord.Intents.default())

        async def failed_scheduler():
            raise RuntimeError("scheduler failed")

        client.reminder_task = asyncio.create_task(failed_scheduler())
        await asyncio.sleep(0)
        with patch.object(bot.discord.Client, "close", new=AsyncMock()) as close:
            with self.assertRaisesRegex(RuntimeError, "scheduler failed"):
                await client.close()

        close.assert_awaited_once()
        self.assertIsNone(client.reminder_task)

    async def test_active_raid_lock_is_shared_and_unused_locks_are_reclaimed(self):
        lock = bot.get_raid_lock(123)
        self.assertIs(lock, bot.get_raid_lock(123))
        await lock.acquire()
        entered = asyncio.Event()

        async def waiter():
            async with bot.get_raid_lock(123):
                entered.set()

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        self.assertFalse(entered.is_set())
        lock.release()
        await task
        del lock
        gc.collect()
        self.assertNotIn(123, bot.raid_locks)


if __name__ == "__main__":
    unittest.main()
