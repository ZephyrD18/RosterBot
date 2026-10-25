import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import aiosqlite

import bot
from test_bot import sample_raid


class RepairMessage:
    def __init__(self, message_id, *, exists=True):
        self.id = message_id
        self.exists = exists
        self.edit_error = None
        self.view = None
        self.embed = None
        self.content = None
        self.allowed_mentions = None

    async def edit(self, *, embed=None, view=None, content=None, allowed_mentions=None):
        if self.edit_error:
            raise self.edit_error
        if not self.exists:
            raise bot.discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Missing post")
        self.embed = embed
        self.view = view
        self.content = content
        self.allowed_mentions = allowed_mentions
        if view is not None:
            bot.bot.add_view(view, message_id=self.id)

    async def delete(self):
        if not self.exists:
            raise bot.discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Missing post")
        self.exists = False


class RepairChannel:
    def __init__(self):
        self.messages = {123: RepairMessage(123), 456: RepairMessage(456)}
        self.created = []

    def get_partial_message(self, message_id):
        return self.messages[message_id]

    async def send(self, content=None, *, embed=None, allowed_mentions=None):
        message = RepairMessage(1000 + len(self.created))
        message.embed = embed
        message.content = content
        message.allowed_mentions = allowed_mentions
        self.messages[message.id] = message
        self.created.append(message)
        return message


class CanonicalRepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db_path = bot.DB_PATH
        self.directory = tempfile.TemporaryDirectory()
        bot.DB_PATH = str(Path(self.directory.name) / "raids.db")
        await bot.init_db()
        self.raid = sample_raid()
        self.raid["signups"] = {"MT": {
            "user_id": 42, "display_name": "Tester", "mention": "<@42>",
            "job": "Warrior", "needs_mount": False, "ready_status": "ready",
        }}
        await bot.insert_raid(self.raid)
        self.channel = RepairChannel()
        self.old_view = bot.RaidSignupView(self.raid)
        bot.bot.add_view(self.old_view, message_id=123)

    async def asyncTearDown(self):
        self.old_view.stop()
        for message in self.channel.messages.values():
            if message.view is not None:
                message.view.stop()
        bot.DB_PATH = self.original_db_path
        self.directory.cleanup()

    async def repair(self, *, repost=False):
        async with bot.get_raid_lock(123):
            raid = await bot.fetch_signup(123)
            return await bot.repair_raid_messages(self.channel, raid, repost=repost)

    async def test_missing_primary_is_recreated_with_saved_players_and_working_buttons(self):
        self.channel.messages[123].exists = False
        warnings = await self.repair()
        self.assertEqual(warnings, [])
        self.assertIsNone(await bot.fetch_signup(123))
        saved = await bot.fetch_signup(1000)
        self.assertEqual(saved["signups"]["MT"]["user_id"], 42)
        self.assertEqual(saved["signups"]["MT"]["job"], "Warrior")
        self.assertFalse(saved["signups"]["MT"]["needs_mount"])
        self.assertEqual(saved["signups"]["MT"]["ready_status"], "ready")
        self.assertIsNone(saved["roster_message_id"])
        self.assertTrue(self.old_view.is_finished())
        view = self.channel.created[0].view
        self.assertIsInstance(view, bot.RaidSignupView)
        self.assertEqual(view.signup_message_id, 1000)
        self.assertTrue(all(":1000:" in child.custom_id for child in view.children))

    async def test_permission_failure_preserves_existing_cards_and_does_not_post_duplicate(self):
        self.channel.messages[123].edit_error = bot.discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Cannot edit",
        )
        with self.assertLogs("rosterbot", level="ERROR"):
            warnings = await self.repair()
        self.assertTrue(warnings)
        self.assertEqual(self.channel.created, [])
        self.assertTrue(self.channel.messages[123].exists)
        self.assertTrue(self.channel.messages[456].exists)
        self.assertEqual((await bot.fetch_signup(123))["roster_message_id"], 456)

    async def test_rejected_database_migration_removes_new_post_and_preserves_original(self):
        async with aiosqlite.connect(bot.DB_PATH) as db:
            await db.execute(
                "CREATE TRIGGER block_repost BEFORE UPDATE OF signup_message_id ON signups "
                "BEGIN SELECT RAISE(ABORT, 'simulated persistence failure'); END",
            )
            await db.commit()
        with self.assertRaisesRegex(aiosqlite.IntegrityError, "simulated persistence failure"):
            await self.repair(repost=True)
        self.assertFalse(self.channel.created[0].exists)
        self.assertTrue(self.channel.messages[123].exists)
        self.assertTrue(self.channel.messages[456].exists)
        self.assertEqual((await bot.fetch_signup(123))["signups"]["MT"]["job"], "Warrior")
        self.assertIsNone(await bot.fetch_signup(1000))

    async def test_legacy_second_card_is_removed_only_after_primary_can_be_refreshed(self):
        warnings = await self.repair()
        self.assertEqual(warnings, [])
        self.assertEqual(self.channel.created, [])
        self.assertTrue(self.channel.messages[123].exists)
        self.assertFalse(self.channel.messages[456].exists)
        self.assertIsNone((await bot.fetch_signup(123))["roster_message_id"])
        self.assertIn("Warrior", self.channel.messages[123].embed.fields[0].value)

    async def test_bump_moves_canonical_post_and_retires_old_buttons(self):
        warnings = await self.repair(repost=True)
        self.assertEqual(warnings, [])
        self.assertFalse(self.channel.messages[123].exists)
        self.assertFalse(self.channel.messages[456].exists)
        self.assertTrue(self.channel.created[0].exists)
        self.assertTrue(self.old_view.is_finished())
        self.assertIsNone(await bot.fetch_signup(123))
        saved = await bot.fetch_signup(1000)
        self.assertEqual(saved["signups"]["MT"]["user_id"], 42)
        self.assertIsNone(saved["roster_message_id"])

    async def save_announcement(self, *, exists=True):
        self.raid["announcement_message_id"] = 789
        self.raid["notify_role_id"] = 77
        self.raid["setup_message"] = "Raid details with @everyone and <@42>."
        await bot.save_raid_state(self.raid)
        self.channel.messages[789] = RepairMessage(789, exists=exists)

    async def test_bump_updates_announcement_to_new_canonical_link_without_pings(self):
        await self.save_announcement()
        warnings = await self.repair(repost=True)
        self.assertEqual(warnings, [])
        announcement = self.channel.messages[789]
        self.assertIn("https://discord.com/channels/1/789/1000", announcement.content)
        self.assertNotIn("/123", announcement.content)
        self.assertTrue(announcement.content.startswith("<@&77> "))
        self.assertEqual(announcement.allowed_mentions.to_dict(), {"parse": []})
        self.assertEqual((await bot.fetch_signup(1000))["announcement_message_id"], 789)

    async def test_healthy_canonical_repair_also_refreshes_announcement_link(self):
        await self.save_announcement()
        warnings = await self.repair()
        self.assertEqual(warnings, [])
        self.assertIn("https://discord.com/channels/1/789/123", self.channel.messages[789].content)
        self.assertEqual(self.channel.messages[789].allowed_mentions.to_dict(), {"parse": []})
        self.assertEqual(self.channel.created, [])

    async def test_missing_announcement_is_recreated_without_pinging_and_saved(self):
        await self.save_announcement(exists=False)
        warnings = await self.repair(repost=True)
        self.assertEqual(warnings, [])
        self.assertEqual(len(self.channel.created), 2)
        replacement = self.channel.created[1]
        self.assertIn("https://discord.com/channels/1/789/1000", replacement.content)
        self.assertEqual(replacement.allowed_mentions.to_dict(), {"parse": []})
        self.assertEqual((await bot.fetch_signup(1000))["announcement_message_id"], replacement.id)

    async def test_unsaved_replacement_announcement_is_rolled_back(self):
        self.raid["roster_message_id"] = None
        await self.save_announcement(exists=False)
        async with aiosqlite.connect(bot.DB_PATH) as db:
            await db.execute(
                "CREATE TRIGGER block_announcement_repair BEFORE UPDATE OF announcement_message_id ON signups "
                "BEGIN SELECT RAISE(ABORT, 'simulated announcement save failure'); END",
            )
            await db.commit()
        with self.assertLogs("rosterbot", level="ERROR"):
            warnings = await self.repair()
        self.assertTrue(warnings)
        self.assertFalse(self.channel.created[0].exists)
        self.assertEqual((await bot.fetch_signup(123))["announcement_message_id"], 789)
