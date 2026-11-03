import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from test_bot import sample_raid


class StandbyTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db_path = bot.DB_PATH
        self.directory = tempfile.TemporaryDirectory()
        bot.DB_PATH = str(Path(self.directory.name) / "raids.db")
        await bot.init_db()
        self.raid = sample_raid()
        self.raid["standby"]["42"] = {
            "user_id": 42, "display_name": "Tester", "mention": "<@42>", "roles": ["Tank"],
        }
        await bot.insert_raid(self.raid)
        self.views = []

    async def asyncTearDown(self):
        for view in self.views:
            view.stop()
        bot.DB_PATH = self.original_db_path
        self.directory.cleanup()

    def event(self):
        return SimpleNamespace(
            user=SimpleNamespace(id=42, display_name="Tester", mention="<@42>"),
            channel=None, channel_id=789,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

    def picker(self, roles=None):
        view = bot.StandbyRoleView(123, 42, roles)
        self.views.append(view)
        return view

    async def test_expiry_removes_private_controls_without_touching_saved_choices(self):
        view = self.picker(["Melee DPS", "Range DPS"])
        message = SimpleNamespace(edit=AsyncMock())
        view.message = message
        with (
            patch.object(bot, "fetch_signup", new=AsyncMock()) as fetch,
            patch.object(bot, "save_raid_state", new=AsyncMock()) as save,
        ):
            await view.on_timeout()
        message.edit.assert_awaited_once_with(
            content="This standby picker expired. Open Standby again on the roster. Your saved choices are unchanged.",
            view=None,
        )
        fetch.assert_not_awaited()
        save.assert_not_awaited()
        self.assertTrue(view.is_finished())
        self.assertIsNone(view.message)
        self.assertEqual((await bot.fetch_signup(123))["standby"]["42"]["roles"], ["Tank"])

    async def test_expiry_handles_a_discord_edit_failure(self):
        view = self.picker()
        response = SimpleNamespace(status=403, reason="Forbidden")
        view.message = SimpleNamespace(edit=AsyncMock(side_effect=bot.discord.Forbidden(response, "Denied")))
        with self.assertLogs("rosterbot", level="ERROR"):
            await view.on_timeout()
        self.assertTrue(view.is_finished())
        self.assertIsNone(view.message)
        self.assertEqual((await bot.fetch_signup(123))["standby"]["42"]["roles"], ["Tank"])

    async def test_opening_picker_retains_the_private_message_for_expiry(self):
        event = self.event()
        message = SimpleNamespace(edit=AsyncMock())
        event.followup.send.return_value = message
        signup = bot.RaidSignupView(self.raid)
        self.views.append(signup)
        await signup.standby_callback(event)
        arguments = event.followup.send.await_args
        picker = arguments.kwargs["view"]
        self.views.append(picker)
        self.assertIs(picker.message, message)
        self.assertTrue(arguments.kwargs["wait"])
        self.assertTrue(arguments.kwargs["ephemeral"])
        self.assertIn("expires after 5 minutes", arguments.args[0])
        self.assertEqual(
            [button.label for button in picker.children[:4]],
            ["Tank", "Healer", "Melee DPS", "Range DPS"],
        )

    async def test_each_category_button_acknowledges_before_loading_saved_state(self):
        original_fetch = bot.fetch_signup
        for category in bot.STANDBY_CHOICES:
            with self.subTest(category=category):
                view = self.picker()
                event = self.event()
                entered, resume = asyncio.Event(), asyncio.Event()

                async def delayed_fetch(signup_message_id, *, event=event, entered=entered, resume=resume):
                    self.assertEqual(event.response.defer.await_count, 1)
                    entered.set()
                    await resume.wait()
                    return await original_fetch(signup_message_id)

                button = next(item for item in view.children if item.label == category)
                with patch.object(bot, "fetch_signup", new=delayed_fetch):
                    task = asyncio.create_task(button.callback(event))
                    try:
                        await asyncio.wait_for(entered.wait(), timeout=2)
                        event.response.defer.assert_awaited_once_with(ephemeral=True, thinking=False)
                        event.edit_original_response.assert_not_awaited()
                    finally:
                        resume.set()
                        await task
                self.assertEqual(view.roles, [category])
                self.assertEqual((await bot.fetch_signup(123))["standby"]["42"]["roles"], ["Tank"])

    async def test_expiry_does_not_replace_a_successful_confirmation(self):
        view = self.picker(["Healer"])
        message = SimpleNamespace(edit=AsyncMock())
        view.message = message
        event = self.event()
        with patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True)):
            await view.confirm_callback(event)
        await view.on_timeout()
        message.edit.assert_not_awaited()
        self.assertEqual((await bot.fetch_signup(123))["standby"]["42"]["roles"], ["Healer"])
        self.assertIn("confirmed", event.edit_original_response.await_args.kwargs["content"])

    async def test_expiry_uses_the_latest_interaction_after_draft_edits(self):
        view = self.picker()
        original, toggled, cleared = [SimpleNamespace(edit=AsyncMock()) for _ in range(3)]
        view.message = original
        event = self.event()
        event.edit_original_response.side_effect = [toggled, cleared]
        await view.toggle_role(event, "Healer")
        self.assertIs(view.message, toggled)
        await view.clear_callback(event)
        self.assertIs(view.message, cleared)
        await view.on_timeout()
        original.edit.assert_not_awaited()
        toggled.edit.assert_not_awaited()
        cleared.edit.assert_awaited_once()
        self.assertEqual((await bot.fetch_signup(123))["standby"]["42"]["roles"], ["Tank"])

    async def test_old_dps_button_reopens_saved_choices_without_committing_a_draft(self):
        legacy = bot.LegacyStandbyChoice(123, 42)
        event = self.event()
        message = SimpleNamespace(edit=AsyncMock())
        event.edit_original_response.return_value = message
        with patch.object(bot, "save_raid_state", new=AsyncMock()) as save:
            await legacy.callback(event)
        event.response.defer.assert_awaited_once_with(ephemeral=True, thinking=False)
        result = event.edit_original_response.await_args.kwargs
        picker = result["view"]
        self.views.append(picker)
        self.assertEqual(picker.roles, ["Tank"])
        self.assertEqual([button.label for button in picker.children[:4]], bot.STANDBY_CHOICES)
        self.assertIs(picker.message, message)
        self.assertIn("saved choices", result["content"])
        save.assert_not_awaited()

    async def test_old_dps_button_rejects_other_players_closed_raids_and_other_channels(self):
        for condition in ("other player", "locked", "deleted", "other channel"):
            with self.subTest(condition=condition):
                event = self.event()
                raid = self.raid.copy()
                if condition == "other player":
                    event.user.id = 99
                elif condition == "locked":
                    raid["locked"] = True
                elif condition == "deleted":
                    raid = None
                else:
                    event.channel_id = 999
                with (
                    patch.object(bot, "fetch_signup", new=AsyncMock(return_value=raid)),
                    patch.object(bot, "save_raid_state", new=AsyncMock()) as save,
                ):
                    await bot.LegacyStandbyChoice(123, 42).callback(event)
                save.assert_not_awaited()
                event.edit_original_response.assert_not_awaited()
                self.assertEqual(event.response.send_message.await_count + event.followup.send.await_count, 1)

    def test_legacy_recovery_matches_no_current_standby_button(self):
        legacy = bot.LegacyStandbyChoice(123, 42)
        view = self.picker()
        self.assertIsNotNone(legacy.template.fullmatch("standby:123:42:DPS"))
        for button in view.children:
            self.assertIsNone(legacy.template.fullmatch(button.custom_id))

    async def test_registered_legacy_button_dispatch_recovers_after_restart(self):
        client = bot.RosterClient(intents=bot.discord.Intents.none())
        client.add_dynamic_items(bot.LegacyStandbyChoice)
        event = self.event()
        event.data = {"component_type": 2, "custom_id": "standby:123:42:DPS"}
        event.message = SimpleNamespace(
            id=999, flags=SimpleNamespace(components_v2=False),
            components=[bot.discord.ActionRow({"type": 1, "components": [{
                "type": 2, "style": 2, "label": "DPS", "custom_id": "standby:123:42:DPS",
            }]})],
        )
        completed = asyncio.Event()

        async def edited(**kwargs):
            self.views.append(kwargs["view"])
            completed.set()
            return SimpleNamespace(edit=AsyncMock())

        event.edit_original_response.side_effect = edited
        store = client._connection._view_store
        store.dispatch_view(2, event.data["custom_id"], event)
        await asyncio.wait_for(completed.wait(), timeout=2)
        event.response.defer.assert_awaited_once()
        self.assertEqual(event.edit_original_response.await_args.kwargs["view"].roles, ["Tank"])
        client.remove_dynamic_items(bot.LegacyStandbyChoice)
