import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

import bot


class CreationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db_path = bot.DB_PATH
        self.directory = tempfile.TemporaryDirectory()
        bot.DB_PATH = str(Path(self.directory.name) / "raids.db")
        await bot.init_db()
        self.messages = [SimpleNamespace(
            id=message_id, jump_url=f"https://discord.test/{message_id}",
            edit=AsyncMock(), delete=AsyncMock(),
        ) for message_id in (123, 456, 789)]
        self.interaction = SimpleNamespace(
            channel=SimpleNamespace(send=AsyncMock(side_effect=self.messages)),
            channel_id=789, guild=SimpleNamespace(roles=[]),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def asyncTearDown(self):
        bot.DB_PATH = self.original_db_path
        self.directory.cleanup()

    async def test_setup_saves_cannot_overwrite_a_waiting_manager(self):
        setup_paused = asyncio.Event()
        resume_setup = asyncio.Event()
        manager_finished = asyncio.Event()
        save_raid_state = bot.save_raid_state
        first_save = True

        async def setup_save(raid):
            nonlocal first_save
            self.assertTrue(bot.get_raid_lock(raid["signup_message_id"]).locked())
            await save_raid_state(raid)
            if first_save:
                first_save = False
                setup_paused.set()
                await resume_setup.wait()

        async def lock_roster():
            async with bot.get_raid_lock(123):
                raid = await bot.fetch_signup(123)
                raid["locked"] = True
                await save_raid_state(raid)
                manager_finished.set()

        with patch.object(bot, "save_raid_state", side_effect=setup_save):
            creation = asyncio.create_task(bot.create_raid(
                self.interaction, "Test raid", "12/31/2099", "8PM", send_announcement=True,
            ))
            await asyncio.wait_for(setup_paused.wait(), timeout=2)
            manager = asyncio.create_task(lock_roster())
            try:
                await asyncio.sleep(0)
                self.assertFalse(manager_finished.is_set())
            finally:
                resume_setup.set()
                await asyncio.gather(creation, manager)

        self.assertTrue((await bot.fetch_signup(123))["locked"])
        self.messages[0].edit.assert_awaited_once()

    async def test_insert_failure_removes_the_unsaved_signup(self):
        with patch.object(bot, "insert_raid", side_effect=aiosqlite.OperationalError("Test failure")):
            with self.assertLogs("rosterbot", level="ERROR"):
                await bot.create_raid(self.interaction, "Test raid", "12/31/2099", "8PM")

        self.messages[0].delete.assert_awaited_once()
        self.assertIsNone(await bot.fetch_signup(123))
        self.assertIn("could not be saved", self.interaction.followup.send.call_args.args[0])

    async def test_roster_save_failure_rolls_back_only_the_roster(self):
        with patch.object(bot, "save_raid_state", side_effect=aiosqlite.OperationalError("Test failure")):
            with self.assertLogs("rosterbot", level="ERROR"):
                await bot.create_raid(self.interaction, "Test raid", "12/31/2099", "8PM")

        self.messages[0].delete.assert_not_awaited()
        self.messages[1].delete.assert_awaited_once()
        self.messages[0].edit.assert_awaited_once()
        self.assertEqual((await bot.fetch_signup(123))["roster_message_id"], 0)

    async def test_announcement_save_failure_still_saves_the_roster(self):
        save_raid_state = bot.save_raid_state
        first_save = True

        async def fail_first_save(raid):
            nonlocal first_save
            if first_save:
                first_save = False
                raise aiosqlite.OperationalError("Test failure")
            await save_raid_state(raid)

        with patch.object(bot, "save_raid_state", side_effect=fail_first_save):
            with self.assertLogs("rosterbot", level="ERROR"):
                await bot.create_raid(
                    self.interaction, "Test raid", "12/31/2099", "8PM", send_announcement=True,
                )

        raid = await bot.fetch_signup(123)
        self.assertIsNone(raid["announcement_message_id"])
        self.assertEqual(raid["roster_message_id"], 789)
        self.messages[1].delete.assert_awaited_once()
        self.messages[0].delete.assert_not_awaited()

    async def test_past_schedule_is_rejected_before_posting(self):
        now = int(datetime.now(timezone.utc).timestamp())
        with patch.object(bot, "parse_raid_datetime", return_value=now - 1):
            await bot.create_raid(self.interaction, "Test raid", "1/1/2020", "8PM")

        self.interaction.channel.send.assert_not_awaited()
        self.assertIn("future", self.interaction.followup.send.call_args.args[0])

    async def test_todays_raid_five_minutes_away_stays_this_year_after_utc_midnight(self):
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).astimezone(tz)

        with patch.object(bot, "datetime", FrozenDatetime):
            await bot.create_raid(self.interaction, "Test raid", "10/24", "5:35PM")

        raid = await bot.fetch_signup(123)
        expected = int(datetime(2026, 10, 25, 0, 35, tzinfo=timezone.utc).timestamp())
        self.assertEqual(raid["scheduled_at"], expected)
        self.assertEqual(raid["scheduled_at"] - int(FrozenDatetime.now(timezone.utc).timestamp()), 300)

    async def test_earlier_today_is_rejected_instead_of_moving_to_next_year(self):
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).astimezone(tz)

        with patch.object(bot, "datetime", FrozenDatetime):
            await bot.create_raid(self.interaction, "Test raid", "10/24", "5:25PM")

        self.interaction.channel.send.assert_not_awaited()
        self.assertIn("future", self.interaction.followup.send.call_args.args[0])
