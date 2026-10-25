import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from test_bot import sample_raid


class StandbyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db_path = bot.DB_PATH
        self.directory = tempfile.TemporaryDirectory()
        bot.DB_PATH = str(Path(self.directory.name) / "raids.db")
        await bot.init_db()
        self.raid = sample_raid()
        await bot.insert_raid(self.raid)
        self.views = []
        self.refresh_patch = patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True))
        self.refresh = self.refresh_patch.start()

    async def asyncTearDown(self):
        for view in self.views:
            view.stop()
        self.refresh_patch.stop()
        bot.DB_PATH = self.original_db_path
        self.directory.cleanup()

    def event(self, user_id=42):
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name="Tester", mention=f"<@{user_id}>"),
            channel=None,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

    def view(self, roles=None):
        view = bot.StandbyRoleView(123, 42, roles)
        self.views.append(view)
        return view

    async def save_standby(self, roles, needs_mount=True):
        self.raid["standby"]["42"] = bot.player_from_user(self.event().user) | {
            "roles": roles,
            "needs_mount": needs_mount,
        }
        await bot.save_raid_state(self.raid)

    async def test_abandoned_picker_keeps_saved_roles_unchanged(self):
        await self.save_standby(["Tank"])
        view = self.view(["Tank"])
        await view.toggle_role(self.event(), "DPS")
        self.assertEqual(view.roles, ["Tank", "DPS"])
        await view.clear_callback(self.event())
        self.assertEqual(view.roles, [])
        view.stop()

        saved = await bot.fetch_signup(123)
        self.assertEqual(saved["standby"]["42"]["roles"], ["Tank"])
        self.refresh.assert_not_awaited()

    async def test_new_draft_does_not_sign_up_until_confirmed(self):
        view = self.view()
        await view.toggle_role(self.event(), "Healer")
        self.assertEqual((await bot.fetch_signup(123))["standby"], {})
        self.refresh.assert_not_awaited()

        event = self.event()
        await view.confirm_callback(event)
        self.assertEqual((await bot.fetch_signup(123))["standby"]["42"]["roles"], ["Healer"])
        self.refresh.assert_awaited_once()
        self.assertEqual(event.followup.send.await_args.args[0], "Do you still need the mount?")
        event.followup.send.await_args.kwargs["view"].stop()

    async def test_confirm_preserves_mount_answer_and_copies_roles(self):
        await self.save_standby(["Tank"], needs_mount=False)
        starting_roles = ["Tank"]
        view = self.view(starting_roles)
        await view.toggle_role(self.event(), "DPS")
        event = self.event()
        await view.confirm_callback(event)

        saved = (await bot.fetch_signup(123))["standby"]["42"]
        self.assertEqual(saved["roles"], ["Tank", "DPS"])
        self.assertFalse(saved["needs_mount"])
        self.assertEqual(starting_roles, ["Tank"])
        self.assertTrue(view.is_finished())
        event.followup.send.assert_not_awaited()

    async def test_clear_removes_saved_standby_only_after_confirm(self):
        await self.save_standby(["Tank"])
        view = self.view(["Tank"])
        await view.clear_callback(self.event())
        self.assertIn("42", (await bot.fetch_signup(123))["standby"])
        event = self.event()
        await view.confirm_callback(event)

        self.assertEqual((await bot.fetch_signup(123))["standby"], {})
        self.assertEqual(event.edit_original_response.await_args.kwargs["content"], "Standby cleared.")
        event.followup.send.assert_not_awaited()

    async def test_lock_between_draft_and_confirm_prevents_save(self):
        view = self.view()
        await view.toggle_role(self.event(), "Tank")
        self.raid["locked"] = True
        await bot.save_raid_state(self.raid)
        event = self.event()
        await view.confirm_callback(event)

        self.assertEqual((await bot.fetch_signup(123))["standby"], {})
        self.assertIn("locked", event.followup.send.await_args.args[0])
        self.refresh.assert_not_awaited()

    async def test_main_signup_between_draft_and_confirm_prevents_standby(self):
        view = self.view()
        await view.toggle_role(self.event(), "Tank")
        self.raid["signups"]["MT"] = bot.player_from_user(self.event().user)
        await bot.save_raid_state(self.raid)
        event = self.event()
        await view.confirm_callback(event)

        self.assertEqual((await bot.fetch_signup(123))["standby"], {})
        self.assertIn("main role", event.followup.send.await_args.args[0])
        self.refresh.assert_not_awaited()

    async def test_concurrent_confirmations_commit_once(self):
        await self.save_standby(["Tank"])
        view = self.view(["Tank", "DPS"])
        first, second = self.event(), self.event()
        with patch.object(bot, "save_raid_state", wraps=bot.save_raid_state) as save:
            await asyncio.gather(view.confirm_callback(first), view.confirm_callback(second))
        save.assert_awaited_once()
        self.refresh.assert_awaited_once()
        self.assertIn("closed", second.followup.send.await_args.args[0])

    async def test_opening_picker_starts_from_saved_roles(self):
        await self.save_standby(["DPS", "Healer"])
        signup_view = bot.RaidSignupView(self.raid)
        self.views.append(signup_view)
        event = self.event()
        await signup_view.standby_callback(event)

        picker = event.followup.send.await_args.kwargs["view"]
        self.views.append(picker)
        self.assertEqual(picker.roles, ["DPS", "Healer"])
        self.assertIn("DPS/Healer", event.followup.send.await_args.args[0])

    async def test_closed_picker_cannot_commit_changes(self):
        view = self.view(["Tank"])
        view.stop()
        event = self.event()
        await view.confirm_callback(event)
        self.assertEqual((await bot.fetch_signup(123))["standby"], {})
        self.assertIn("closed", event.followup.send.await_args.args[0])

    async def test_picker_rejects_another_user(self):
        view = self.view(["Tank"])
        event = self.event(99)
        await view.toggle_role(event, "DPS")
        await view.clear_callback(event)
        await view.confirm_callback(event)
        self.assertEqual(view.roles, ["Tank"])
        self.assertEqual(event.response.send_message.await_count, 3)
        self.assertEqual((await bot.fetch_signup(123))["standby"], {})
