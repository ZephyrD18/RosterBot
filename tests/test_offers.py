import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from test_bot import sample_raid


class StandbyOfferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db_path = bot.DB_PATH
        self.directory = tempfile.TemporaryDirectory()
        bot.DB_PATH = str(Path(self.directory.name) / "raids.db")
        await bot.init_db()
        self.raid = sample_raid()
        self.raid["signups"] = {
            key: {"user_id": index + 100, "mention": f"<@{index + 100}>"}
            for index, (key, *_) in enumerate(bot.ROLES) if key != "MT"
        }
        self.raid["standby"] = {
            str(user_id): {
                "user_id": user_id, "display_name": f"Player {user_id}", "mention": f"<@{user_id}>",
                "roles": ["Tank"], "needs_mount": False, "job": "Warrior", "ready_status": None,
            }
            for user_id in (42, 43)
        }
        self.raid["standby_offers"] = {}
        await bot.insert_raid(self.raid)
        self.messages = []

        async def send(*args, **kwargs):
            message = SimpleNamespace(id=1000 + len(self.messages), edit=AsyncMock(), delete=AsyncMock())
            self.messages.append((message, args, kwargs))
            return message

        self.channel = SimpleNamespace(send=AsyncMock(side_effect=send))
        self.channel.get_partial_message = lambda message_id: next(message for message, _, _ in self.messages if message.id == message_id)
        self.refresh_patch = patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True))
        self.refresh_patch.start()

    async def asyncTearDown(self):
        self.refresh_patch.stop()
        bot.stop_standby_offer_views(123)
        for _, _, kwargs in self.messages:
            kwargs["view"].stop()
        bot.DB_PATH = self.original_db_path
        self.directory.cleanup()

    def event(self, user_id=42):
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name=f"Player {user_id}", mention=f"<@{user_id}>"),
            channel=self.channel,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

    async def open_offer(self):
        async with bot.get_raid_lock(123):
            raid = await bot.fetch_signup(123)
            await bot.offer_standby_for_open_slots(self.channel, raid)
        return self.messages[-1][2]["view"]

    async def test_offer_is_compatible_private_to_recipient_and_durable(self):
        view = await self.open_offer()
        saved = await bot.fetch_signup(123)
        self.assertEqual(saved["standby_offers"]["MT"]["user_id"], 42)
        self.assertEqual(saved["standby_offers"]["MT"]["message_id"], 1000)
        self.assertNotIn("MT", saved["signups"])
        self.assertIn("42", saved["standby"])
        self.assertTrue(view.is_persistent())
        allowed = self.messages[0][2]["allowed_mentions"]
        self.assertFalse(allowed.roles)
        self.assertFalse(allowed.everyone)
        self.assertEqual([user.id for user in allowed.users], [42])

    async def test_other_player_cannot_accept(self):
        view = await self.open_offer()
        event = self.event(43)
        await view.respond(event, True)
        event.response.send_message.assert_awaited_once()
        self.assertNotIn("MT", (await bot.fetch_signup(123))["signups"])

    async def test_accept_preserves_job_and_mount_and_commits_only_once(self):
        view = await self.open_offer()
        first, second = self.event(), self.event()
        await asyncio.gather(view.respond(first, True), view.respond(second, True))
        saved = await bot.fetch_signup(123)
        self.assertEqual(saved["signups"]["MT"]["user_id"], 42)
        self.assertEqual(saved["signups"]["MT"]["job"], "Warrior")
        self.assertFalse(saved["signups"]["MT"]["needs_mount"])
        self.assertNotIn("42", saved["standby"])
        self.assertEqual(saved["standby_offers"], {})
        self.assertIn("no longer", second.followup.send.await_args.args[0])

    async def test_accept_clears_incompatible_job_and_opens_clickable_job_menu(self):
        self.raid["standby"]["42"]["job"] = "White Mage"
        await bot.save_raid_state(self.raid)
        view = await self.open_offer()
        event = self.event()
        await view.respond(event, True)
        saved = await bot.fetch_signup(123)
        self.assertIsNone(saved["signups"]["MT"]["job"])
        picker = event.followup.send.await_args.kwargs["view"]
        try:
            self.assertIsInstance(picker, bot.JobChoiceView)
            self.assertEqual(picker.role_key, "MT")
        finally:
            picker.stop()

    async def test_decline_advances_to_next_candidate_without_repeated_pings(self):
        first = await self.open_offer()
        await first.respond(self.event(), False)
        saved = await bot.fetch_signup(123)
        self.assertEqual(saved["standby_offers"]["MT"]["user_id"], 43)
        second = self.messages[-1][2]["view"]
        await second.respond(self.event(43), False)
        saved = await bot.fetch_signup(123)
        self.assertIsNone(saved["standby_offers"]["MT"]["user_id"])
        self.assertEqual(set(saved["standby"]), {"42", "43"})
        await bot.process_standby_offers(self.channel, saved)
        self.assertEqual(self.channel.send.await_count, 2)
        self.assertFalse(bot.standby_work_due(saved, 2**32))

    async def test_expiration_advances_without_changing_saved_roster(self):
        await self.open_offer()
        raid = await bot.fetch_signup(123)
        expires_at = raid["standby_offers"]["MT"]["expires_at"]
        with patch.object(bot, "datetime") as clock:
            clock.now.return_value = datetime.fromtimestamp(expires_at, timezone.utc)
            await bot.process_standby_offers(self.channel, raid)
        saved = await bot.fetch_signup(123)
        self.assertEqual(saved["standby_offers"]["MT"]["user_id"], 43)
        self.assertNotIn("MT", saved["signups"])
        self.assertEqual(set(saved["standby"]), {"42", "43"})

    async def test_expired_acceptance_does_not_claim_slot(self):
        view = await self.open_offer()
        raid = await bot.fetch_signup(123)
        with patch.object(bot, "datetime") as clock:
            clock.now.return_value = datetime.fromtimestamp(raid["standby_offers"]["MT"]["expires_at"], timezone.utc)
            event = self.event()
            await view.respond(event, True)
        self.assertIn("no longer", event.followup.send.await_args.args[0])
        self.assertNotIn("MT", (await bot.fetch_signup(123))["signups"])

    async def test_later_offer_rejects_old_generation(self):
        view = await self.open_offer()
        raid = await bot.fetch_signup(123)
        raid["standby_offers"]["MT"]["token"] = "different"
        await bot.save_raid_state(raid)
        event = self.event()
        await view.respond(event, True)
        self.assertIn("no longer", event.followup.send.await_args.args[0])
        self.assertNotIn("MT", (await bot.fetch_signup(123))["signups"])

    async def test_restart_restores_saved_buttons(self):
        original = await self.open_offer()
        ids = [child.custom_id for child in original.children]
        original.stop()
        bot.restore_standby_offer_views(await bot.fetch_signup(123))
        restored = next(view for view in bot.bot.persistent_views if isinstance(view, bot.StandbyOfferView))
        self.assertEqual([child.custom_id for child in restored.children], ids)

    async def test_failed_notification_retries_same_candidate(self):
        response = SimpleNamespace(status=503, reason="Unavailable")
        send = self.channel.send.side_effect
        self.channel.send.side_effect = bot.discord.HTTPException(response, "Unavailable")
        with self.assertLogs("rosterbot", level="ERROR"):
            await bot.offer_standby_for_open_slots(self.channel, self.raid)
        pending = await bot.fetch_signup(123)
        self.assertEqual(pending["standby_offers"]["MT"]["user_id"], 42)
        self.assertIsNone(pending["standby_offers"]["MT"]["message_id"])
        self.channel.send.side_effect = send
        await bot.process_standby_offers(self.channel, pending)
        saved = await bot.fetch_signup(123)
        self.assertEqual(saved["standby_offers"]["MT"]["user_id"], 42)
        self.assertEqual(saved["standby_offers"]["MT"]["message_id"], 1000)

    async def test_locked_or_filled_slot_invalidates_offer(self):
        view = await self.open_offer()
        raid = await bot.fetch_signup(123)
        raid["locked"] = True
        await bot.save_raid_state(raid)
        event = self.event()
        await view.respond(event, True)
        self.assertNotIn("MT", (await bot.fetch_signup(123))["signups"])
        await bot.process_standby_offers(self.channel, raid)
        self.assertEqual((await bot.fetch_signup(123))["standby_offers"], {})

    async def test_one_player_is_not_offered_multiple_open_slots(self):
        self.raid["signups"] = {}
        self.raid["standby"].pop("43")
        await bot.save_raid_state(self.raid)
        await bot.offer_standby_for_open_slots(self.channel, self.raid)
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertEqual(set((await bot.fetch_signup(123))["standby_offers"]), {"MT"})


class StandbySelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_choices_visually_track_selection_without_changing_saved_state(self):
        view = bot.StandbyRoleView(123, 42, ["Tank"])
        try:
            buttons = {button.label: button for button in view.children}
            self.assertEqual(buttons["Tank"].style, bot.discord.ButtonStyle.primary)
            self.assertEqual(buttons["DPS"].style, bot.discord.ButtonStyle.secondary)
            self.assertIn("Clear selections", buttons)
            view.roles = ["DPS"]
            view.update_selection_styles()
            self.assertEqual(buttons["Tank"].style, bot.discord.ButtonStyle.secondary)
            self.assertEqual(buttons["DPS"].style, bot.discord.ButtonStyle.primary)
        finally:
            view.stop()

    def test_malformed_saved_offers_are_ignored(self):
        raw = bot.encode_json({
            "MT": {"user_id": 42, "expires_at": 1_000_000, "message_id": 456, "token": "abc123ef", "tried_user_ids": [42, 42, True, "43"]},
            "OT": {"user_id": "bad", "token": "abc123ef"},
            "H1": [],
            "M1": {"user_id": 42, "expires_at": None, "token": "abc123ef"},
        })
        saved = bot.decode_standby_offers(raw)
        self.assertEqual(set(saved), {"MT"})
        self.assertEqual(saved["MT"]["tried_user_ids"], [42])
