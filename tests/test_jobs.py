import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import bot
from test_bot import sample_raid
from test_interactions import interaction


class JobChoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db_path = bot.DB_PATH
        self.directory = tempfile.TemporaryDirectory()
        bot.DB_PATH = str(Path(self.directory.name) / "raids.db")
        await bot.init_db()
        self.event, _ = interaction()
        self.event.response.send_message = AsyncMock()
        self.raid = sample_raid() | {"roster_message_id": None}
        self.raid["signups"]["MT"] = bot.player_from_user(self.event.user)
        await bot.insert_raid(self.raid)

    async def asyncTearDown(self):
        bot.DB_PATH = self.original_db_path
        self.directory.cleanup()

    def test_every_slot_has_only_appropriate_clickable_jobs(self):
        expected = {
            "MT": ("Paladin", "Warrior", "Dark Knight", "Gunbreaker"),
            "OT": ("Paladin", "Warrior", "Dark Knight", "Gunbreaker"),
            "H1": ("White Mage", "Scholar", "Astrologian", "Sage"),
            "H2": ("White Mage", "Scholar", "Astrologian", "Sage"),
            "M1": ("Monk", "Dragoon", "Ninja", "Samurai", "Reaper", "Viper"),
            "M2": ("Monk", "Dragoon", "Ninja", "Samurai", "Reaper", "Viper"),
            "R1": ("Bard", "Machinist", "Dancer"),
            "R2": ("Black Mage", "Summoner", "Red Mage", "Pictomancer"),
        }
        for role_key, jobs in expected.items():
            with self.subTest(role_key=role_key):
                view = bot.JobChoiceView(123, 42, role_key)
                self.assertEqual(tuple(option.value for option in view.menu.options), jobs)
                self.assertEqual(view.menu.min_values, 1)
                self.assertEqual(view.menu.max_values, 1)
                view.stop()
        self.assertEqual(len(bot.ALL_JOBS), 21)

    async def test_warrior_choice_opens_clickable_mount_question_and_no_is_saved(self):
        view = bot.JobChoiceView(123, 42, "MT")
        with patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True)):
            await view.set_job(self.event, "Warrior")
            self.assertEqual((await bot.fetch_signup(123))["signups"]["MT"]["job"], "Warrior")
            message = self.event.edit_original_response.await_args.kwargs
            self.assertIn("Do you still need the mount?", message["content"])
            mount = message["view"]
            self.assertEqual([button.label for button in mount.children], ["Yes", "No"])
            await mount.set_mount_status(self.event, False)
        saved = (await bot.fetch_signup(123))["signups"]["MT"]
        self.assertEqual(saved["job"], "Warrior")
        self.assertFalse(saved["needs_mount"])
        mount.stop()

    async def test_main_role_click_opens_job_menu_before_mount(self):
        view = bot.RaidSignupView(self.raid)
        with (
            patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True)),
            patch.object(bot, "add_raid_role", new=AsyncMock()),
        ):
            await view.handle_role(self.event, "OT", "Off Tank")
        picker = self.event.followup.send.await_args.kwargs["view"]
        self.assertIsInstance(picker, bot.JobChoiceView)
        self.assertEqual(picker.role_key, "OT")
        self.assertEqual(set((await bot.fetch_signup(123))["signups"]), {"OT"})
        picker.stop()
        view.stop()

    async def test_tank_slot_rejects_dps_job_without_changing_saved_state(self):
        view = bot.JobChoiceView(123, 42, "MT")
        await view.set_job(self.event, "Black Mage")
        self.assertIsNone((await bot.fetch_signup(123))["signups"]["MT"]["job"])
        self.assertIn("does not fit", self.event.followup.send.await_args.args[0])
        view.stop()

    async def test_picker_rejects_another_player(self):
        view = bot.JobChoiceView(123, 99, "MT")
        await view.set_job(self.event, "Warrior")
        self.assertIsNone((await bot.fetch_signup(123))["signups"]["MT"]["job"])
        self.event.response.send_message.assert_awaited_once()
        view.stop()

    async def test_stale_picker_cannot_change_new_slot(self):
        view = bot.JobChoiceView(123, 42, "MT")
        self.raid["signups"]["M1"] = self.raid["signups"].pop("MT")
        await bot.save_raid_state(self.raid)
        await view.set_job(self.event, "Warrior")
        self.assertIsNone((await bot.fetch_signup(123))["signups"]["M1"]["job"])
        self.assertIn("signup changed", self.event.followup.send.await_args.args[0])
        view.stop()

    async def test_completed_raid_rejects_job_changes_and_disables_all_buttons(self):
        self.raid["finished_at"] = 1234
        self.raid["locked"] = True
        await bot.save_raid_state(self.raid)
        picker = bot.JobChoiceView(123, 42, "MT")
        await picker.set_job(self.event, "Warrior")
        self.assertIsNone((await bot.fetch_signup(123))["signups"]["MT"]["job"])
        view = bot.RaidSignupView(self.raid)
        self.assertTrue(all(item.disabled for item in view.children))
        picker.stop()
        view.stop()

    async def test_locked_roster_still_allows_current_player_to_finish_job_details(self):
        self.raid["locked"] = True
        await bot.save_raid_state(self.raid)
        view = bot.JobChoiceView(123, 42, "MT")
        with patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True)):
            await view.set_job(self.event, "Warrior")
        self.assertEqual((await bot.fetch_signup(123))["signups"]["MT"]["job"], "Warrior")
        self.event.edit_original_response.await_args.kwargs["view"].stop()

    async def test_standby_job_menu_is_filtered_to_confirmed_roles(self):
        self.raid["signups"] = {}
        self.raid["standby"]["42"] = bot.player_from_user(self.event.user) | {"roles": ["Healer"]}
        await bot.save_raid_state(self.raid)
        view = bot.JobChoiceView(123, 42, None, ["Healer"])
        self.assertEqual(tuple(option.value for option in view.menu.options), bot.JOB_GROUPS["Healer"])
        with patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True)):
            await view.set_job(self.event, "Sage")
        self.assertEqual((await bot.fetch_signup(123))["standby"]["42"]["job"], "Sage")
        self.event.edit_original_response.await_args.kwargs["view"].stop()
