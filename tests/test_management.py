import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiosqlite

import bot
from test_bot import sample_raid
from test_interactions import interaction


class ManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db_path = bot.DB_PATH
        self.directory = tempfile.TemporaryDirectory()
        bot.DB_PATH = str(Path(self.directory.name) / "raids.db")
        await bot.init_db()
        self.raid = sample_raid() | {
            "scheduled_at": 2_000_000_000,
            "finished_at": None,
            "ready_check_started_at": 100,
            "signups": {
                "MT": {
                    "user_id": 42, "display_name": "Tester", "mention": "<@42>",
                    "job": "Warrior", "needs_mount": False, "ready_status": "ready",
                },
            },
        }
        await bot.insert_raid(self.raid)
        self.event, self.role = interaction()
        self.event.channel.guild = self.event.guild
        self.event.channel.send = AsyncMock()

    async def asyncTearDown(self):
        bot.DB_PATH = self.original_db_path
        self.directory.cleanup()

    async def save_legacy_completion(self, finished_at):
        # Completion records are retained from the retired feature, not created by current commands.
        async with aiosqlite.connect(bot.DB_PATH) as db:
            await db.execute("UPDATE signups SET finished_at = ?, locked = 1 WHERE signup_message_id = 123", (finished_at,))
            await db.commit()

    async def test_reschedule_preserves_signup_job_and_mount_and_resets_readiness(self):
        self.raid["reminders_sent"] = {"automatic": True, "five_minute": True}
        await bot.save_raid_state(self.raid)
        with patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True)):
            await bot.editraid.callback(self.event, date="12/31/2099", time="8PM")
        saved = await bot.fetch_signup(123)
        self.assertEqual(saved["scheduled_at"], bot.parse_raid_datetime("12/31/2099", "8PM", "PT"))
        self.assertEqual(saved["signups"]["MT"]["job"], "Warrior")
        self.assertFalse(saved["signups"]["MT"]["needs_mount"])
        self.assertIsNone(saved["signups"]["MT"]["ready_status"])
        self.assertIsNone(saved["ready_check_started_at"])
        self.assertEqual(saved["reminders_sent"], {})
        self.event.channel.send.assert_awaited_once()
        self.assertFalse(self.event.channel.send.await_args.kwargs["allowed_mentions"].roles)

    async def test_text_edit_does_not_reset_reminders_or_ready_check(self):
        self.raid["reminders_sent"] = {"automatic": True}
        await bot.save_raid_state(self.raid)
        with patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=False)):
            await bot.editraid.callback(self.event, raid_name="Weekly clear", meeting_server="-", group="Team A")
        saved = await bot.fetch_signup(123)
        self.assertEqual(saved["raid_name"], "Weekly clear")
        self.assertIsNone(saved["meeting_server"])
        self.assertEqual(saved["group_name"], "Team A")
        self.assertEqual(saved["reminders_sent"], {"automatic": True})
        self.assertEqual(saved["ready_check_started_at"], 100)
        self.assertIn(bot.ROSTER_REFRESH_WARNING, self.event.followup.send.await_args.args[0])
        self.event.channel.send.assert_not_awaited()

    async def test_reschedule_deletes_old_start_reminder_before_saving_new_time(self):
        self.raid["start_reminder_message_ids"] = [555]
        self.raid["reminders_sent"] = {"five_minute": True}
        await bot.save_raid_state(self.raid)
        message = SimpleNamespace(delete=AsyncMock())
        self.event.channel.get_partial_message = Mock(return_value=message)
        with patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True)):
            await bot.editraid.callback(self.event, date="12/31/2099", time="8PM")
        message.delete.assert_awaited_once()
        self.event.channel.get_partial_message.assert_called_once_with(555)
        saved = await bot.fetch_signup(123)
        self.assertEqual(saved["start_reminder_message_ids"], [])
        self.assertEqual(saved["reminders_sent"], {})
        self.assertEqual(saved["scheduled_at"], bot.parse_raid_datetime("12/31/2099", "8PM", "PT"))

    async def test_failed_old_reminder_cleanup_preserves_schedule_and_reminder_flags(self):
        self.raid["start_reminder_message_ids"] = [555]
        self.raid["reminders_sent"] = {"automatic": True, "five_minute": True}
        await bot.save_raid_state(self.raid)
        with (
            patch.object(bot, "delete_start_reminders", new=AsyncMock(return_value=False)) as cleanup,
            patch.object(bot, "update_raid_messages", new=AsyncMock()) as refresh,
        ):
            await bot.editraid.callback(self.event, date="12/31/2099", time="8PM")
        cleanup.assert_awaited_once()
        refresh.assert_not_awaited()
        self.event.channel.send.assert_not_awaited()
        saved = await bot.fetch_signup(123)
        self.assertEqual(saved["scheduled_at"], self.raid["scheduled_at"])
        self.assertEqual(saved["start_reminder_message_ids"], [555])
        self.assertEqual(saved["reminders_sent"], self.raid["reminders_sent"])
        self.assertEqual(saved["ready_check_started_at"], 100)
        self.assertIn("schedule was not changed", self.event.followup.send.await_args.args[0])

    async def test_invalid_edits_do_not_change_saved_raid(self):
        for arguments, expected in (
            ({"date": "12/31/2099"}, "both date and time"),
            ({"date": "1/1/2000", "time": "8PM"}, "future"),
            ({"raid_name": " "}, "cannot be blank"),
        ):
            with self.subTest(arguments=arguments):
                await bot.editraid.callback(self.event, **arguments)
                self.assertIn(expected, self.event.followup.send.await_args.args[0])
                self.assertEqual((await bot.fetch_signup(123))["scheduled_at"], self.raid["scheduled_at"])
        self.event.channel.send.assert_not_awaited()

    async def test_finished_raid_cannot_be_edited_or_ready_checked(self):
        await self.save_legacy_completion(101)
        for command, arguments in ((bot.editraid, {"raid_name": "Changed"}), (bot.readycheck, {})):
            await command.callback(self.event, **arguments)
            self.assertIn("Only an active raid", self.event.followup.send.await_args.args[0])
        self.event.channel.send.assert_not_awaited()

    async def test_ready_check_clears_previous_answers_and_accepts_only_signed_players(self):
        with patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True)):
            await bot.readycheck.callback(self.event)
            saved = await bot.fetch_signup(123)
            self.assertIsNone(saved["signups"]["MT"]["ready_status"])
            self.assertNotIn("view", self.event.channel.send.await_args.kwargs)
            view = bot.ReadyCheckView(123, saved["ready_check_started_at"])
            try:
                await view.set_status(self.event, "late")
                self.assertEqual((await bot.fetch_signup(123))["signups"]["MT"]["ready_status"], "late")
                self.event.user.id = 43
                await view.set_status(self.event, "ready")
                self.assertIn("Only players", self.event.followup.send.await_args.args[0])
                self.assertEqual((await bot.fetch_signup(123))["signups"]["MT"]["ready_status"], "late")
            finally:
                view.stop()

    async def test_old_ready_check_cannot_overwrite_new_check(self):
        view = bot.ReadyCheckView(123, 99)
        try:
            await view.set_status(self.event, "unavailable")
            self.assertIn("has ended", self.event.followup.send.await_args.args[0])
            self.assertEqual((await bot.fetch_signup(123))["signups"]["MT"]["ready_status"], "ready")
        finally:
            view.stop()

    async def test_new_checks_in_same_second_have_distinct_generations(self):
        with (
            patch.object(bot, "datetime") as clock,
            patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True)),
        ):
            clock.now.return_value = datetime.fromtimestamp(100, timezone.utc)
            await bot.readycheck.callback(self.event)
            first = (await bot.fetch_signup(123))["ready_check_started_at"]
            await bot.readycheck.callback(self.event)
            second = (await bot.fetch_signup(123))["ready_check_started_at"]
        self.assertEqual(first, 101)
        self.assertEqual(second, 102)

    async def test_completed_roster_rejects_unlock_clear_lock_and_role_changes(self):
        await self.save_legacy_completion(1234)
        for command, arguments in (
            (bot.unlockroster, ()), (bot.clearroster, ()),
            (bot.lockroster, ()), (bot.raidrole, (self.role,)),
        ):
            with self.subTest(command=command.name):
                with (
                    patch.object(bot, "save_raid_state", new=AsyncMock()) as save,
                    patch.object(bot, "update_raid_messages", new=AsyncMock()) as refresh,
                    patch.object(bot, "add_raid_role", new=AsyncMock()) as add,
                ):
                    await command.callback(self.event, *arguments)
                save.assert_not_awaited()
                refresh.assert_not_awaited()
                add.assert_not_awaited()
                self.assertIn("completed", self.event.followup.send.await_args.args[0].lower())
        saved = await bot.fetch_signup(123)
        self.assertTrue(saved["locked"])
        self.assertEqual(saved["signups"]["MT"]["job"], "Warrior")
        self.assertEqual(saved["finished_at"], 1234)

    async def test_manual_reminder_rechecks_completed_state_after_resolving(self):
        async def resolve(*_):
            snapshot = await bot.fetch_signup(123)
            await self.save_legacy_completion(1234)
            return snapshot, None

        with patch.object(bot, "resolve_raid_for_command", side_effect=resolve):
            await bot.remindraid.callback(self.event)
        self.event.channel.send.assert_not_awaited()
        self.assertIn("cannot send reminders", self.event.followup.send.await_args.args[0])

    def test_fixed_cleanup_has_no_finish_command_or_duration_setting(self):
        self.assertNotIn("finishraid", {command.name for command in bot.tree.get_commands()})
        for command in (bot.raidsetup, bot.raidpreset, bot.editraid):
            self.assertNotIn("duration_minutes", {parameter.name for parameter in command.parameters})

