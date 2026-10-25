import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

import bot


def make_role(guild, role_id=10, name="Raid", *, mentionable=False, **permissions):
    return discord.Role(guild=guild, state=None, data={
        "id": str(role_id), "name": name, "position": 1,
        "permissions": str(discord.Permissions(**permissions).value),
        "mentionable": mentionable,
    })


class RoleSecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(id=1, owner_id=99, channels=[], roles=[])
        self.role = make_role(self.guild)
        self.guild.roles = [self.role]
        self.guild.get_role = lambda role_id: self.role if role_id == self.role.id else None
        self.guild.default_role = make_role(self.guild, 1, "@everyone", view_channel=True, send_messages=True)
        self.member = SimpleNamespace(id=42, guild=self.guild, add_roles=AsyncMock(), remove_roles=AsyncMock())
        self.raid = {
            "signup_message_id": 123, "raid_name": "Test raid",
            "raid_role_id": self.role.id, "raid_role_name": self.role.name,
            "signups": {"MT": {"user_id": 42, "mention": "<@42>"}},
        }

    def channel_with_overwrite(self, **permissions):
        channel = discord.TextChannel(guild=self.guild, state=None, data={
            "id": "20", "name": "raid-room", "position": 0, "type": 0,
            "permission_overwrites": [
                {"id": "1", "type": 0, "allow": "0", "deny": str(discord.Permissions(view_channel=True).value)},
                {"id": "10", "type": 0, "allow": str(discord.Permissions(**permissions).value), "deny": "0"},
            ],
        })
        self.guild.channels = [channel]
        return channel

    async def test_channel_moderation_overwrites_cannot_be_self_assigned(self):
        for permission in ("manage_messages", "manage_channels", "manage_roles", "manage_webhooks", "manage_threads", "mention_everyone"):
            with self.subTest(permission=permission):
                self.channel_with_overwrite(**{permission: True, "view_channel": True})
                warning = await bot.add_raid_role(self.member, self.raid)
                self.assertIn("channel-specific", warning)
        self.member.add_roles.assert_not_awaited()

    async def test_private_raid_channel_access_is_allowed(self):
        channel = self.channel_with_overwrite(view_channel=True)
        self.assertFalse(channel.permissions_for(self.guild.default_role).view_channel)
        self.assertTrue(channel.permissions_for(self.role).view_channel)
        self.assertIsNone(await bot.add_raid_role(self.member, self.raid))
        self.member.add_roles.assert_awaited_once()

    async def test_privileged_role_is_not_removed_by_roster_cleanup(self):
        self.role._permissions = discord.Permissions(manage_roles=True).value
        with patch.object(bot, "save_role_update", new=AsyncMock()):
            warning = await bot.remove_raid_role(self.member, self.raid)
        self.assertIn("server manager", warning)
        self.member.remove_roles.assert_not_awaited()

    async def test_channel_moderation_role_is_not_removed_by_roster_cleanup(self):
        self.channel_with_overwrite(manage_messages=True)
        with patch.object(bot, "save_role_update", new=AsyncMock()):
            warning = await bot.remove_raid_role(self.member, self.raid)
        self.assertIn("channel-specific", warning)
        self.member.remove_roles.assert_not_awaited()

    async def test_missing_pinned_role_never_assigns_or_removes_same_named_role(self):
        self.raid["raid_role_id"] = 11
        self.assertIsNone(bot.raid_role_for_guild(self.guild, self.raid))
        self.assertIn("could not find", await bot.add_raid_role(self.member, self.raid))
        self.assertIsNone(await bot.remove_raid_role(self.member, self.raid))
        self.member.add_roles.assert_not_awaited()
        self.member.remove_roles.assert_not_awaited()

    def test_legacy_roster_without_role_id_can_resolve_by_name(self):
        self.raid["raid_role_id"] = None
        self.assertIs(bot.raid_role_for_guild(self.guild, self.raid), self.role)

    def test_reminders_only_ping_signed_players(self):
        self.role.mentionable = True
        self.raid["standby"] = {"43": {"user_id": 43, "mention": "<@43>"}}
        reminder = bot.five_minute_reminder_text(self.raid | {"scheduled_at": 123})
        self.assertIn("<@42>", reminder)
        self.assertNotIn("<@43>", reminder)
        self.assertNotIn(self.role.mention, reminder)
        allowed = bot.allowed_mentions_for_raid(self.raid)
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.roles)
        self.assertEqual([user.id for user in allowed.users], [42])


class NotificationSecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(id=1, roles=[])
        self.role = make_role(self.guild, name=bot.DEFAULT_NOTIFY_ROLE_NAME)
        self.guild.roles = [self.role]
        messages = [SimpleNamespace(id=message_id, jump_url="https://discord.test/signup", edit=AsyncMock())
                    for message_id in (123, 124, 125)]
        self.event = SimpleNamespace(
            guild=self.guild, channel_id=789,
            channel=SimpleNamespace(send=AsyncMock(side_effect=messages)),
            permissions=discord.Permissions(manage_messages=True),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def create(self, *, default_role=False):
        with patch.object(bot, "insert_raid", new=AsyncMock()) as insert, \
             patch.object(bot, "save_raid_state", new=AsyncMock()):
            await bot.create_raid(
                self.event, "Test raid", "12/31/2099", "8PM",
                notify_role=None if default_role else self.role, send_announcement=True,
            )
        return insert

    async def test_nonmentionable_role_requires_actor_permission_before_posting(self):
        insert = await self.create()
        self.event.channel.send.assert_not_awaited()
        insert.assert_not_awaited()
        self.assertIn("Mention Everyone", self.event.followup.send.await_args.args[0])

    async def test_default_notification_role_has_the_same_authorization(self):
        insert = await self.create(default_role=True)
        self.event.channel.send.assert_not_awaited()
        insert.assert_not_awaited()

    async def test_mentionable_role_does_not_require_mention_everyone(self):
        self.role.mentionable = True
        insert = await self.create()
        insert.assert_awaited_once()
        announcement = self.event.channel.send.await_args_list[1]
        self.assertIn(self.role.mention, announcement.args[0])
        self.assertEqual(announcement.kwargs["allowed_mentions"].roles, [self.role])
        self.assertFalse(announcement.kwargs["allowed_mentions"].everyone)

    async def test_actor_with_permission_can_ping_nonmentionable_role(self):
        self.event.permissions.mention_everyone = True
        insert = await self.create()
        insert.assert_awaited_once()
        self.assertEqual(self.event.channel.send.await_count, 2)

    async def test_everyone_is_rejected_even_with_mention_permission(self):
        self.role = make_role(self.guild, 1, "@everyone", mentionable=True)
        self.event.permissions.mention_everyone = True
        insert = await self.create()
        self.event.channel.send.assert_not_awaited()
        insert.assert_not_awaited()
        self.assertIn("other than @everyone", self.event.followup.send.await_args.args[0])
