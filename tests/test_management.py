import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
            "duration_minutes": 180,
            "finished_at": None,
            "ready_check_started_at": 100,
            "standby_offers": {},
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

    async def test_reschedule_preserves_signup_job_and_mount_and_resets_readiness(self):
        self.raid["reminders_sent"] = {"automatic": True, "five_minute": True}
        await bot.save_raid_state(self.raid)
        with patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True)):
            await bot.editraid.callback(self.event, date="12/31/2099", time="8PM", duration_minutes=240)
        saved = await bot.fetch_signup(123)
        self.assertEqual(saved["duration_minutes"], 240)
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

    async def test_invalid_edits_do_not_change_saved_raid(self):
        for arguments, expected in (
            ({"date": "12/31/2099"}, "both date and time"),
            ({"duration_minutes": 0}, "Duration must"),
            ({"date": "1/1/2000", "time": "8PM"}, "future"),
            ({"raid_name": " "}, "cannot be blank"),
        ):
            with self.subTest(arguments=arguments):
                await bot.editraid.callback(self.event, **arguments)
                self.assertIn(expected, self.event.followup.send.await_args.args[0])
                self.assertEqual((await bot.fetch_signup(123))["scheduled_at"], self.raid["scheduled_at"])
        self.event.channel.send.assert_not_awaited()

    async def test_finished_raid_cannot_be_edited_or_ready_checked(self):
        self.raid["finished_at"] = 101
        await bot.save_raid_state(self.raid)
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

    async def test_automatic_finish_keeps_roster_and_removes_roles(self):
        now = self.raid["scheduled_at"] + 180 * 60
        with (
            patch.object(bot, "datetime") as clock,
            patch.object(bot, "fetch_raid_channel", new=AsyncMock(return_value=self.event.channel)),
            patch.object(bot, "queue_raid_role_cleanup", new=AsyncMock(return_value=[])) as cleanup,
            patch.object(bot, "delete_raid_messages", new=AsyncMock()) as delete,
            patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True)),
        ):
            clock.now.return_value = datetime.fromtimestamp(now, timezone.utc)
            await bot.process_due_raid(123)
        saved = await bot.fetch_signup(123)
        self.assertEqual(saved["finished_at"], now)
        self.assertTrue(saved["locked"])
        self.assertEqual(saved["signups"]["MT"]["job"], "Warrior")
        cleanup.assert_awaited_once()
        delete.assert_not_awaited()
        self.assertFalse(bot.reminder_work_due(saved, now + 60))

    async def test_finish_without_channel_still_uses_saved_server(self):
        with (
            patch.object(bot.bot, "get_guild", return_value=self.event.guild),
            patch.object(bot, "queue_raid_role_cleanup", new=AsyncMock(return_value=["Retry queued"])) as cleanup,
            patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=False)),
        ):
            warnings = await bot.finish_raid(None, self.raid)
        self.assertIsNotNone((await bot.fetch_signup(123))["finished_at"])
        self.assertIs(cleanup.await_args.args[0], self.event.guild)
        self.assertIn("Retry queued", warnings)
        self.assertIn(bot.ROSTER_REFRESH_WARNING, warnings)

    async def test_finish_command_is_idempotent(self):
        with (
            patch.object(bot, "queue_raid_role_cleanup", new=AsyncMock(return_value=[])) as cleanup,
            patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True)),
        ):
            await bot.finishraid.callback(self.event)
            await bot.finishraid.callback(self.event)
        cleanup.assert_awaited_once()
        self.assertIn("already finished", self.event.followup.send.await_args.args[0])

    async def test_completed_roster_rejects_unlock_clear_lock_and_role_changes(self):
        self.raid["finished_at"] = 1234
        self.raid["locked"] = True
        await bot.save_raid_state(self.raid)
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
            self.raid["finished_at"] = 1234
            self.raid["locked"] = True
            await bot.save_raid_state(self.raid)
            return snapshot, None

        with patch.object(bot, "resolve_raid_for_command", side_effect=resolve):
            await bot.remindraid.callback(self.event)
        self.event.channel.send.assert_not_awaited()
        self.assertIn("cannot send reminders", self.event.followup.send.await_args.args[0])

    async def test_shortened_duration_cannot_end_raid_in_the_past(self):
        now = self.raid["scheduled_at"] + 60 * 60
        with (
            patch.object(bot, "datetime") as clock,
            patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True)),
        ):
            clock.now.return_value = datetime.fromtimestamp(now, timezone.utc)
            await bot.editraid.callback(self.event, duration_minutes=30)
            self.assertIn("end the raid in the past", self.event.followup.send.await_args.args[0])
            self.assertEqual((await bot.fetch_signup(123))["duration_minutes"], 180)
            await bot.editraid.callback(self.event, duration_minutes=120)
        saved = await bot.fetch_signup(123)
        self.assertEqual(saved["duration_minutes"], 120)
        self.assertIsNone(saved["finished_at"])

    async def test_interrupted_finish_recovers_role_cleanup_after_restart(self):
        with patch.object(bot, "queue_raid_role_cleanup", new=AsyncMock(side_effect=RuntimeError("Interrupted"))):
            with self.assertRaisesRegex(RuntimeError, "Interrupted"):
                await bot.finish_raid(self.event.channel, self.raid)
        saved = await bot.fetch_signup(123)
        finished_at = saved["finished_at"]
        self.assertTrue(saved["role_cleanup_pending"])
        self.assertTrue(bot.reminder_work_due(saved, finished_at + 60))
        self.assertIn(123, {raid["signup_message_id"] for raid in await bot.fetch_scheduled_raids(0)})
        await bot.init_db()
        with (
            patch.object(bot, "fetch_raid_channel", new=AsyncMock(return_value=self.event.channel)),
            patch.object(bot, "queue_raid_role_cleanup", new=AsyncMock(return_value=[])) as cleanup,
            patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True)),
        ):
            await bot.process_due_raid(123)
        saved = await bot.fetch_signup(123)
        cleanup.assert_awaited_once()
        self.assertFalse(saved["role_cleanup_pending"])
        self.assertEqual(saved["finished_at"], finished_at)

    async def test_role_cleanup_warning_keeps_finished_raid_due_for_retry(self):
        with (
            patch.object(bot, "queue_raid_role_cleanup", new=AsyncMock(return_value=["Role removal queued"])),
            patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True)),
        ):
            await bot.finish_raid(self.event.channel, self.raid)
        saved = await bot.fetch_signup(123)
        self.assertTrue(saved["role_cleanup_pending"])
        self.assertTrue(bot.reminder_work_due(saved, saved["finished_at"] + 60))
        with (
            patch.object(bot, "queue_raid_role_cleanup", new=AsyncMock(return_value=["Role removal queued"])),
            patch.object(bot, "save_raid_state", new=AsyncMock(wraps=bot.save_raid_state)) as save,
            patch.object(bot, "update_raid_messages", new=AsyncMock()) as refresh,
        ):
            await bot.finish_raid(self.event.channel, saved)
        save.assert_not_awaited()
        refresh.assert_not_awaited()

