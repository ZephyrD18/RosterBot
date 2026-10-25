import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

import bot


def raid(**changes):
    return {
        "signup_message_id": 123,
        "roster_message_id": 456,
        "channel_id": 789,
        "raid_name": "Test raid",
        "locked": False,
        "signups": {},
        "standby": {},
        "raid_role_id": 10,
        "raid_role_name": "Raid",
    } | changes


def interaction(*, manage_roles=True):
    guild = SimpleNamespace(id=1, owner_id=99, roles=[])

    def make_role(role_id, position, **permissions):
        return discord.Role(
            guild=guild,
            state=None,
            data={
                "id": str(role_id),
                "name": "Raid",
                "position": position,
                "permissions": str(discord.Permissions(**permissions).value),
            },
        )

    role = make_role(10, 1)
    guild.roles = [role]
    guild.get_role = lambda role_id: next((role for role in guild.roles if role.id == role_id), None)
    guild.me = SimpleNamespace(top_role=make_role(40, 4))
    member = Mock(spec=discord.Member)
    member.id = 42
    member.display_name = "Tester"
    member.mention = "<@42>"
    member.guild = guild
    member.roles = [role]
    member.top_role = make_role(30, 3)
    member.guild_permissions = discord.Permissions(manage_messages=True, manage_roles=manage_roles)
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return SimpleNamespace(
        guild=guild,
        channel=SimpleNamespace(),
        channel_id=789,
        user=member,
        permissions=discord.Permissions(manage_messages=True),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    ), role


def http_error():
    return discord.HTTPException(SimpleNamespace(status=500, reason="Test failure"), "Unavailable")


class RoleSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_role_command_requires_manage_roles(self):
        event, role = interaction(manage_roles=False)
        with patch.object(bot, "resolve_raid_for_command", new=AsyncMock(return_value=(raid(), None))), \
             patch.object(bot, "save_raid_state", new=AsyncMock()) as save:
            await bot.raidrole.callback(event, role)
        save.assert_not_awaited()
        self.assertIn("Manage Roles", event.followup.send.await_args.args[0])

    async def test_role_command_respects_actor_hierarchy(self):
        event, role = interaction()
        event.user.top_role = role
        with patch.object(bot, "resolve_raid_for_command", new=AsyncMock(return_value=(raid(), None))), \
             patch.object(bot, "save_raid_state", new=AsyncMock()) as save:
            await bot.raidrole.callback(event, role)
        save.assert_not_awaited()
        self.assertIn("below your highest", event.followup.send.await_args.args[0])

    async def test_privileged_default_or_legacy_role_cannot_be_self_assigned(self):
        for permission in ("administrator", "manage_roles", "manage_messages", "moderate_members", "mention_everyone"):
            with self.subTest(permission=permission):
                event, role = interaction()
                role._permissions = discord.Permissions(**{permission: True}).value
                warning = await bot.add_raid_role(event.user, raid())
                self.assertIn("cannot grant", warning)
                event.user.add_roles.assert_not_awaited()

    async def test_failed_assignment_returns_warning_after_saved_signup(self):
        event, _ = interaction()
        event.user.add_roles.side_effect = http_error()
        warning = await bot.add_raid_role(event.user, raid())
        self.assertIn("Your signup was saved", warning)

    async def test_cached_roles_do_not_skip_atomic_assignment(self):
        event, role = interaction()
        self.assertIsNone(await bot.add_raid_role(event.user, raid()))
        event.user.add_roles.assert_awaited_once_with(role, reason="Signed up for Test raid")

    async def test_shared_role_remains_while_another_roster_needs_it(self):
        event, _ = interaction()
        other = raid(signup_message_id=124, signups={"MT": {"user_id": 42}})
        with patch.object(bot, "fetch_raids_for_role", new=AsyncMock(return_value=[raid(), other])):
            self.assertIsNone(await bot.remove_raid_role(event.user, raid()))
        event.user.remove_roles.assert_not_awaited()

    async def test_last_roster_removes_role_even_with_stale_member_cache(self):
        event, role = interaction()
        event.user.roles = []
        with patch.object(bot, "fetch_raids_for_role", new=AsyncMock(return_value=[raid()])):
            self.assertIsNone(await bot.remove_raid_role(event.user, raid()))
        event.user.remove_roles.assert_awaited_once_with(role, reason="Left Test raid")

    async def test_legacy_role_name_from_another_guild_does_not_retain_role(self):
        event, role = interaction()
        other = raid(signup_message_id=124, raid_role_id=None, signups={"MT": {"user_id": 42}})
        channel = SimpleNamespace(guild=SimpleNamespace(id=2))
        with patch.object(bot, "fetch_raids_for_role", new=AsyncMock(return_value=[other])), \
             patch.object(bot, "fetch_raid_channel", new=AsyncMock(return_value=channel)):
            await bot.remove_raid_role(event.user, raid())
        event.user.remove_roles.assert_awaited_once_with(role, reason="Left Test raid")

    async def test_cleanup_reports_member_lookup_failure(self):
        event, _ = interaction()
        with patch.object(bot, "fetch_guild_member", new=AsyncMock(side_effect=http_error())), \
             self.assertLogs("rosterbot", level="ERROR"):
            warning = await bot.remove_raid_role_from_player(event.guild, raid(), {"user_id": 42})
        self.assertIn("may still need removal", warning)

    async def test_role_command_reports_member_lookup_failure(self):
        event, role = interaction()
        saved_raid = raid(signups={"MT": {"user_id": 42}})
        with patch.object(bot, "resolve_raid_for_command", new=AsyncMock(return_value=(saved_raid, None))), \
             patch.object(bot, "fetch_signup", new=AsyncMock(return_value=saved_raid)), \
             patch.object(bot, "save_raid_state", new=AsyncMock()), \
             patch.object(bot, "update_raid_messages", new=AsyncMock()), \
             patch.object(bot, "fetch_guild_member", new=AsyncMock(side_effect=http_error())), \
             self.assertLogs("rosterbot", level="ERROR"):
            await bot.raidrole.callback(event, role)
        self.assertIn("may still need updating", event.followup.send.await_args.args[0])

    def test_channel_permission_overwrite_is_respected(self):
        event, _ = interaction()
        event.permissions = discord.Permissions.none()
        self.assertFalse(bot.require_roster_manager(event)[0])


class InteractionStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_standby_picker_cannot_clear_locked_roster(self):
        event, _ = interaction()
        view = bot.StandbyRoleView(123, 42)
        saved_raid = raid(locked=True, standby={"42": {"roles": ["Tank"]}})
        with patch.object(bot, "fetch_signup", new=AsyncMock(return_value=saved_raid)), \
             patch.object(bot, "save_raid_state", new=AsyncMock()) as save:
            await view.clear_callback(event)
        save.assert_not_awaited()
        self.assertIn("locked", event.followup.send.await_args.args[0])
        view.stop()

    async def test_out_of_range_message_ids_do_not_reach_sqlite(self):
        event, _ = interaction()
        with patch.object(bot, "fetch_signup", new=AsyncMock()) as fetch:
            for message_id in ("0", "-1", str(2**63), "9" * 200):
                result, error = await bot.resolve_raid_for_command(event, message_id)
                self.assertIsNone(result)
                self.assertIn("valid", error)
        fetch.assert_not_awaited()

    async def test_legacy_roster_without_channel_is_not_accessible_cross_channel(self):
        event, _ = interaction()
        with patch.object(bot, "fetch_signup", new=AsyncMock(return_value=raid(channel_id=None))):
            result, error = await bot.resolve_raid_for_command(event, "123")
        self.assertIsNone(result)
        self.assertIn("different channel", error)

    async def test_pending_cleanup_can_only_be_resolved_for_deletion(self):
        event, _ = interaction()
        saved_raid = raid(cleanup_pending=True)
        with patch.object(bot, "fetch_signup", new=AsyncMock(return_value=saved_raid)):
            result, error = await bot.resolve_raid_for_command(event, "123")
            self.assertIsNone(result)
            self.assertIn("cleanup is pending", error)
            result, error = await bot.resolve_raid_for_command(event, "123", allow_cleanup_pending=True)
            self.assertIs(result, saved_raid)
            self.assertIsNone(error)

    async def test_roster_commands_are_guild_only(self):
        for command in bot.tree.get_commands():
            if command.name != "help":
                self.assertTrue(command.guild_only, command.name)

    async def test_message_updates_use_known_ids_without_fetching(self):
        message = SimpleNamespace(edit=AsyncMock())
        channel = SimpleNamespace(get_partial_message=Mock(return_value=message))
        with patch.object(bot, "signup_embed", return_value=None), \
             patch.object(bot, "roster_embed", return_value=None):
            self.assertTrue(await bot.update_raid_messages(channel, raid()))
        self.assertEqual([call.args[0] for call in channel.get_partial_message.call_args_list], [123, 456])
        self.assertEqual(message.edit.await_count, 2)

    async def test_partial_post_deletion_keeps_saved_roster_and_roles(self):
        event, _ = interaction()
        with patch.object(bot, "fetch_signup", new=AsyncMock(return_value=raid())), \
             patch.object(bot, "delete_raid_messages", new=AsyncMock(return_value=False)), \
             patch.object(bot, "delete_raid_row", new=AsyncMock()) as delete, \
             patch.object(bot, "remove_all_raid_roles", new=AsyncMock()) as remove:
            deleted, warnings = await bot.delete_raid_for_command(event, raid())
        self.assertFalse(deleted)
        self.assertTrue(warnings)
        delete.assert_not_awaited()
        remove.assert_not_awaited()

    async def test_saved_roster_is_marked_pending_before_shared_role_cleanup(self):
        event, _ = interaction()
        stages = []

        async def delete_row(_):
            stages.append("delete")

        async def mark_pending(saved_raid):
            stages.append("pending")
            saved_raid["cleanup_pending"] = True

        async def remove_roles(*_):
            stages.append("roles")
            self.assertTrue(_[1]["cleanup_pending"])
            return []

        with patch.object(bot, "fetch_signup", new=AsyncMock(return_value=raid())), \
             patch.object(bot, "delete_raid_messages", new=AsyncMock(return_value=True)), \
             patch.object(bot, "mark_raid_cleanup_pending", new=mark_pending), \
             patch.object(bot, "delete_raid_row", new=delete_row), \
             patch.object(bot, "remove_all_raid_roles", new=remove_roles):
            self.assertEqual(await bot.delete_raid_for_command(event, raid()), (True, []))
        self.assertEqual(stages, ["pending", "roles", "delete"])


if __name__ == "__main__":
    unittest.main()
