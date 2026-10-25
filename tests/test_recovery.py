import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from test_bot import sample_raid
from test_interactions import interaction


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db_path = bot.DB_PATH
        self.directory = tempfile.TemporaryDirectory()
        bot.DB_PATH = str(Path(self.directory.name) / "raids.db")
        await bot.init_db()
        self.raid = sample_raid()
        await bot.insert_raid(self.raid)
        self.event, self.role = interaction()
        self.message = SimpleNamespace(id=999, delete=AsyncMock(), edit=AsyncMock())
        self.event.channel.send = AsyncMock(return_value=self.message)
        self.event.channel.get_partial_message = lambda _: self.message

    async def asyncTearDown(self):
        bot.DB_PATH = self.original_db_path
        self.directory.cleanup()

    async def test_bump_rechecks_pending_cleanup_after_resolving(self):
        async def resolve(*_):
            snapshot = await bot.fetch_signup(123)
            await bot.mark_raid_cleanup_pending(snapshot.copy())
            return snapshot, None

        with patch.object(bot, "resolve_raid_for_command", side_effect=resolve):
            await bot.bumproster.callback(self.event)

        self.event.channel.send.assert_not_awaited()
        self.assertIn("cleanup is pending", self.event.followup.send.await_args.args[0])
        self.assertEqual((await bot.fetch_signup(123))["roster_message_id"], 456)

    async def test_manager_commands_report_failed_post_refresh(self):
        commands = (
            (bot.lockroster, ()),
            (bot.unlockroster, ()),
            (bot.clearroster, ()),
            (bot.raidrole, (self.role,)),
        )
        for command, arguments in commands:
            with self.subTest(command=command.name):
                with (
                    patch.object(bot, "resolve_raid_for_command", new=AsyncMock(return_value=(self.raid, None))),
                    patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=False)),
                ):
                    await command.callback(self.event, *arguments)
                self.assertIn(bot.ROSTER_REFRESH_WARNING, self.event.followup.send.await_args.args[0])

    async def test_signup_and_resign_report_failed_post_refresh(self):
        self.raid["standby"]["42"] = bot.player_from_user(self.event.user) | {
            "roles": ["Tank"], "needs_mount": False,
        }
        await bot.save_raid_state(self.raid)
        view = bot.RaidSignupView(self.raid)
        try:
            with patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=False)):
                await view.handle_role(self.event, "MT", "Main Tank")
                self.assertTrue(any(
                    bot.ROSTER_REFRESH_WARNING in call.args[0]
                    for call in self.event.followup.send.await_args_list
                ))
                picker = self.event.followup.send.await_args.kwargs["view"]
                self.assertIsInstance(picker, bot.JobChoiceView)
                picker.stop()
                saved = await bot.fetch_signup(123)
                self.assertEqual(saved["signups"]["MT"]["user_id"], 42)
                self.assertFalse(saved["signups"]["MT"]["needs_mount"])
                self.assertEqual(saved["standby"], {})

                await view.resign_callback(self.event)
                self.assertIn(bot.ROSTER_REFRESH_WARNING, self.event.followup.send.await_args.args[0])
                self.assertEqual((await bot.fetch_signup(123))["signups"], {})
        finally:
            view.stop()

    async def test_standby_confirm_and_clear_report_failed_post_refresh(self):
        self.raid["standby"]["42"] = bot.player_from_user(self.event.user) | {
            "roles": ["Tank"], "needs_mount": False,
        }
        await bot.save_raid_state(self.raid)
        for selected_roles in (["DPS"], []):
            with self.subTest(selected_roles=selected_roles):
                view = bot.StandbyRoleView(123, 42, selected_roles)
                try:
                    with patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=False)):
                        await view.confirm_callback(self.event)
                    message = self.event.edit_original_response.await_args.kwargs["content"]
                    self.assertIn(bot.ROSTER_REFRESH_WARNING, message)
                    standby = (await bot.fetch_signup(123))["standby"]
                    if selected_roles:
                        self.assertEqual(standby["42"]["roles"], selected_roles)
                        self.assertFalse(standby["42"]["needs_mount"])
                    else:
                        self.assertEqual(standby, {})
                finally:
                    view.stop()

    async def test_mount_change_reports_failed_post_refresh(self):
        self.raid["signups"]["MT"] = bot.player_from_user(self.event.user) | {"needs_mount": False}
        await bot.save_raid_state(self.raid)
        view = bot.MountNeedView(123, 42)
        try:
            with patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=False)):
                await view.set_mount_status(self.event, True)
            message = self.event.edit_original_response.await_args.kwargs["content"]
            self.assertIn("Needs Mount", message)
            self.assertIn(bot.ROSTER_REFRESH_WARNING, message)
            self.assertTrue((await bot.fetch_signup(123))["signups"]["MT"]["needs_mount"])
        finally:
            view.stop()
