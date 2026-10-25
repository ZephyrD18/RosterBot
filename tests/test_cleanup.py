import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from test_bot import sample_raid


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db_path = bot.DB_PATH
        self.directory = tempfile.TemporaryDirectory()
        bot.DB_PATH = str(Path(self.directory.name) / "raids.db")
        await bot.init_db()
        self.raid = sample_raid()
        self.raid["raid_role_id"] = 10
        self.raid["signups"] = {"MT": {"user_id": 42, "needs_mount": True}}
        await bot.insert_raid(self.raid)
        self.event = SimpleNamespace(channel=object(), guild=object())

    async def asyncTearDown(self):
        bot.DB_PATH = self.original_db_path
        self.directory.cleanup()

    async def test_failed_role_cleanup_survives_restart_and_retries_without_redeleting_posts(self):
        with (
            patch.object(bot, "delete_raid_messages", new=AsyncMock(return_value=True)) as posts,
            patch.object(bot, "remove_all_raid_roles", new=AsyncMock(return_value=["Discord unavailable"])),
        ):
            deleted, warnings = await bot.delete_raid_for_command(self.event, self.raid)
        self.assertFalse(deleted)
        self.assertIn("retry automatically", warnings[-1])
        posts.assert_awaited_once()
        saved = await bot.fetch_signup(123)
        self.assertTrue(saved["cleanup_pending"])
        self.assertTrue(saved["locked"])
        self.assertEqual(saved["signups"]["MT"]["user_id"], 42)

        await bot.init_db()
        channel = SimpleNamespace(guild=object())
        with (
            patch.object(bot, "fetch_raid_channel", new=AsyncMock(return_value=channel)),
            patch.object(bot, "delete_raid_messages", new=AsyncMock()) as posts,
            patch.object(bot, "remove_all_raid_roles", new=AsyncMock(return_value=[])),
            patch.object(bot, "datetime") as clock,
        ):
            clock.now.return_value = datetime.fromtimestamp(1_000_000, timezone.utc)
            await bot.send_due_reminders()
        posts.assert_not_awaited()
        self.assertIsNone(await bot.fetch_signup(123))

    async def test_missing_server_does_not_discard_pending_role_cleanup(self):
        await bot.mark_raid_cleanup_pending(self.raid)
        with patch.object(bot, "remove_all_raid_roles", new=AsyncMock()) as remove:
            deleted, warnings = await bot.finish_raid_cleanup(None, self.raid)
        self.assertFalse(deleted)
        self.assertIn("remains saved", warnings[0])
        self.assertIsNotNone(await bot.fetch_signup(123))
        remove.assert_not_awaited()

    async def test_pending_rows_are_hidden_from_active_rosters_but_due_even_without_schedule(self):
        await bot.mark_raid_cleanup_pending(self.raid)
        other = sample_raid() | {"signup_message_id": 124, "scheduled_at": None, "raid_role_id": 10}
        await bot.insert_raid(other)
        await bot.mark_raid_cleanup_pending(other)

        self.assertIsNone(await bot.fetch_latest_signup_for_channel(789))
        self.assertEqual(await bot.fetch_all_raids(), [])
        self.assertEqual(await bot.fetch_raids_for_role(10, bot.DEFAULT_RAID_ROLE_NAME), [])
        self.assertEqual({raid["signup_message_id"] for raid in await bot.fetch_scheduled_raids(0)}, {123, 124})
        self.assertTrue(bot.reminder_work_due(await bot.fetch_signup(124), 0))

    async def test_stale_roster_save_cannot_erase_pending_cleanup_members(self):
        stale = await bot.fetch_signup(123)
        await bot.mark_raid_cleanup_pending(self.raid)
        stale["signups"] = {}
        stale["locked"] = False
        with self.assertRaisesRegex(ValueError, "awaiting role cleanup"):
            await bot.save_raid_state(stale)
        saved = await bot.fetch_signup(123)
        self.assertTrue(saved["cleanup_pending"])
        self.assertTrue(saved["locked"])
        self.assertEqual(saved["signups"]["MT"]["user_id"], 42)

    async def test_concurrent_deletions_release_last_shared_role(self):
        other = sample_raid() | {"signup_message_id": 124, "raid_role_id": 10, "signups": self.raid["signups"]}
        await bot.insert_raid(other)
        role = SimpleNamespace(id=10, name=bot.DEFAULT_RAID_ROLE_NAME)
        member = SimpleNamespace(id=42, remove_roles=AsyncMock())
        guild = SimpleNamespace(id=1, get_role=lambda _: role, get_member=lambda _: member)
        member.guild = guild
        event = SimpleNamespace(guild=guild, channel=object())
        with patch.object(bot, "delete_raid_messages", new=AsyncMock(return_value=True)):
            results = await asyncio.gather(
                bot.delete_raid_for_command(event, self.raid),
                bot.delete_raid_for_command(event, other),
            )

        self.assertEqual(results, [(True, []), (True, [])])
        self.assertGreaterEqual(member.remove_roles.await_count, 1)
        self.assertIs(member.remove_roles.await_args.args[0], role)
        self.assertIsNone(await bot.fetch_signup(123))
        self.assertIsNone(await bot.fetch_signup(124))
