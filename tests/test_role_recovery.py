import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
import discord

import bot
from test_bot import sample_raid
from test_interactions import interaction


class RoleRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db_path = bot.DB_PATH
        self.directory = tempfile.TemporaryDirectory()
        bot.DB_PATH = str(Path(self.directory.name) / "raids.db")
        await bot.init_db()
        self.guild = SimpleNamespace(id=1, channels=[], roles=[])
        self.role = self.make_role(10, "Raid")
        self.guild.roles = [self.role]
        self.guild.get_role = lambda role_id: next((r for r in self.guild.roles if r.id == role_id), None)
        self.member = SimpleNamespace(id=42, guild=self.guild, add_roles=AsyncMock(), remove_roles=AsyncMock())
        self.guild.get_member = lambda user_id: self.member if user_id == 42 else None
        self.raid = sample_raid() | {
            "raid_role_id": 10, "raid_role_name": "Raid",
            "signups": {"MT": {"user_id": 42, "mention": "<@42>", "needs_mount": False}},
        }
        await bot.insert_raid(self.raid)

    async def asyncTearDown(self):
        bot.DB_PATH = self.original_db_path
        self.directory.cleanup()

    def make_role(self, role_id, name, **permissions):
        return discord.Role(guild=self.guild, state=None, data={
            "id": str(role_id), "name": name, "position": 1,
            "permissions": str(discord.Permissions(**permissions).value),
        })

    def http_error(self):
        return discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "Retry later")

    async def pending(self):
        async with aiosqlite.connect(bot.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM role_retries ORDER BY role_id") as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def retry(self):
        with patch.object(bot.bot, "get_guild", return_value=self.guild):
            return await bot.retry_pending_role_updates()

    async def test_failed_assignment_survives_restart_and_retries(self):
        self.member.add_roles.side_effect = self.http_error()
        warning = await bot.add_raid_role(self.member, self.raid)
        self.assertIn("retry", warning)
        self.assertEqual((await self.pending())[0]["action"], "add")
        await bot.init_db()
        self.member.add_roles.side_effect = None
        self.assertEqual(await self.retry(), [])
        self.assertEqual(self.member.add_roles.await_count, 2)
        self.assertEqual(await self.pending(), [])

    async def test_failed_resignation_cleanup_retries_after_saved_membership_is_gone(self):
        self.raid["signups"] = {}
        await bot.save_raid_state(self.raid)
        self.member.remove_roles.side_effect = self.http_error()
        self.assertIn("saved", await bot.remove_raid_role(self.member, self.raid))
        self.member.remove_roles.side_effect = None
        await self.retry()
        self.assertEqual(self.member.remove_roles.await_count, 2)
        self.assertEqual(await self.pending(), [])

    async def test_pending_remove_cannot_strip_a_rejoined_member(self):
        await bot.queue_role_update(1, 42, 10, "Raid", "remove", "Left raid")
        await self.retry()
        self.member.remove_roles.assert_not_awaited()
        self.member.add_roles.assert_awaited_once_with(self.role, reason="Left raid")
        self.assertEqual(await self.pending(), [])

    async def test_retry_rechecks_membership_after_waiting_for_role_lock(self):
        self.raid["signups"] = {}
        await bot.save_raid_state(self.raid)
        await bot.queue_role_update(1, 42, 10, "Raid", "remove", "Left raid")
        lock = bot.get_role_lock(42, 10)
        await lock.acquire()
        task = asyncio.create_task(bot.retry_role_update({"guild_id": 1, "user_id": 42, "role_id": 10}))
        try:
            with patch.object(bot.bot, "get_guild", return_value=self.guild):
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.raid["signups"] = {"MT": {"user_id": 42}}
                await bot.save_raid_state(self.raid)
                lock.release()
                await task
        finally:
            if lock.locked():
                lock.release()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.member.remove_roles.assert_not_awaited()
        self.member.add_roles.assert_awaited_once()
        self.assertEqual(await self.pending(), [])

    async def test_stale_assignment_reconciles_to_removal_after_raid_is_deleted(self):
        await bot.queue_role_update(1, 42, 10, "Raid", "add", "Joined raid")
        await bot.delete_raid_row(123)
        await self.retry()
        self.member.add_roles.assert_not_awaited()
        self.member.remove_roles.assert_awaited_once()
        self.assertEqual(await self.pending(), [])

    async def test_finished_roster_does_not_keep_a_role_assigned(self):
        self.raid["finished_at"] = 1_000_000
        self.raid["locked"] = True
        await bot.save_raid_state(self.raid)
        self.member.remove_roles.side_effect = self.http_error()
        warnings = await bot.queue_raid_role_cleanup(self.guild, self.raid)
        self.assertIn("retry", warnings[0])
        self.member.remove_roles.side_effect = None
        await self.retry()
        self.member.remove_roles.assert_awaited()
        self.member.add_roles.assert_not_awaited()
        self.assertEqual(await self.pending(), [])

    async def test_missing_server_keeps_pending_updates(self):
        await bot.queue_role_update(1, 42, 10, "Raid", "add", "Joined raid")
        with patch.object(bot.bot, "get_guild", return_value=None):
            warnings = await bot.retry_pending_role_updates()
        self.assertIn("cannot access", warnings[0])
        self.assertEqual(len(await self.pending()), 1)

    async def test_deleted_role_cannot_redirect_to_same_named_replacement(self):
        await bot.queue_role_update(1, 42, 10, "Raid", "add", "Joined raid")
        self.guild.roles = [self.make_role(11, "Raid")]
        await self.retry()
        self.member.add_roles.assert_not_awaited()
        self.member.remove_roles.assert_not_awaited()
        self.assertEqual(await self.pending(), [])

    async def test_pending_privileged_role_requires_manager_review(self):
        await bot.queue_role_update(1, 42, 10, "Raid", "remove", "Left raid")
        self.role._permissions = discord.Permissions(manage_roles=True).value
        warnings = await self.retry()
        self.assertIn("manager", warnings[0])
        self.member.remove_roles.assert_not_awaited()
        self.assertEqual(len(await self.pending()), 1)

    async def test_membership_lookup_failure_is_saved_for_cleanup_retry(self):
        with (
            patch.object(bot, "fetch_guild_member", new=AsyncMock(side_effect=self.http_error())),
            self.assertLogs("rosterbot", level="ERROR"),
        ):
            warning = await bot.remove_raid_role_from_player(self.guild, self.raid, {"user_id": 42})
        self.assertIn("saved", warning)
        self.assertEqual((await self.pending())[0]["action"], "remove")

    async def test_member_who_left_server_does_not_keep_queue_entry(self):
        await bot.queue_role_update(1, 42, 10, "Raid", "remove", "Left raid")
        with patch.object(bot, "fetch_guild_member", new=AsyncMock(return_value=None)):
            await self.retry()
        self.assertEqual(await self.pending(), [])

    async def test_old_role_waits_until_replacement_assignment_succeeds(self):
        replacement = self.make_role(11, "New Raid")
        self.guild.roles.append(replacement)
        self.raid.update(raid_role_id=11, raid_role_name="New Raid")
        await bot.save_raid_state(self.raid)
        await bot.queue_role_change(self.guild, 42, replacement, self.role, self.raid)
        self.member.add_roles.side_effect = self.http_error()
        await self.retry()
        self.member.remove_roles.assert_not_awaited()
        self.assertEqual(len(await self.pending()), 2)
        self.member.add_roles.side_effect = None
        await self.retry()
        self.member.remove_roles.assert_awaited_once_with(self.role, reason="Left Recollection EX")
        self.assertEqual(await self.pending(), [])

    async def test_role_change_lookup_failure_keeps_both_role_ids(self):
        event, old_role = interaction()
        replacement = discord.Role(guild=event.guild, state=None, data={
            "id": "11", "name": "New Raid", "position": 1, "permissions": "0",
        })
        event.guild.roles.append(replacement)
        with (
            patch.object(bot, "fetch_guild_member", new=AsyncMock(side_effect=self.http_error())),
            patch.object(bot, "update_raid_messages", new=AsyncMock(return_value=True)),
            self.assertLogs("rosterbot", level="ERROR"),
        ):
            await bot.raidrole.callback(event, replacement)
        pending = await self.pending()
        self.assertEqual([(row["role_id"], row["action"]) for row in pending], [(10, "remove"), (11, "add")])
        self.assertEqual(pending[0]["replacement_role_id"], replacement.id)

    async def test_replacement_wait_uses_current_membership_after_another_raid_leaves(self):
        replacement = self.make_role(11, "New Raid")
        self.guild.roles.append(replacement)
        self.raid.update(raid_role_id=11, raid_role_name="New Raid")
        await bot.save_raid_state(self.raid)
        await bot.queue_role_change(self.guild, 42, replacement, self.role, self.raid)
        await bot.queue_role_update(1, 42, 11, "New Raid", "remove", "Another raid ended")
        await self.retry()
        self.member.remove_roles.assert_not_awaited()
        self.member.add_roles.assert_awaited_once_with(replacement, reason="Another raid ended")
        await self.retry()
        self.member.remove_roles.assert_awaited_once_with(self.role, reason="Left Recollection EX")
        self.assertEqual(await self.pending(), [])

    async def test_repair_command_repairs_posts_and_never_adds_roles_to_finished_roster(self):
        self.raid.update(finished_at=1_000_000, locked=True)
        await bot.save_raid_state(self.raid)
        event, _ = interaction()
        with (
            patch.object(bot, "repair_raid_messages", new=AsyncMock(return_value=[])) as repair,
            patch.object(bot, "queue_raid_role_cleanup", new=AsyncMock(return_value=[])) as cleanup,
            patch.object(bot, "retry_pending_role_updates", new=AsyncMock(return_value=[])),
            patch.object(bot, "add_raid_role", new=AsyncMock()) as add,
        ):
            await bot.repairraid.callback(event)
        repair.assert_awaited_once()
        cleanup.assert_awaited_once()
        add.assert_not_awaited()
        self.assertIn("Repairs are complete", event.followup.send.await_args.args[0])

    async def test_repair_command_requires_manager_permissions(self):
        event, _ = interaction()
        event.permissions = discord.Permissions.none()
        with patch.object(bot, "repair_raid_messages", new=AsyncMock()) as repair:
            await bot.repairraid.callback(event)
        repair.assert_not_awaited()
        self.assertIn("Manage Messages", event.followup.send.await_args.args[0])
