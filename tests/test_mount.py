import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


def make_raid(*, standby=False, locked=False, needs_mount=None):
    player = {"user_id": 42, "needs_mount": needs_mount}
    return {
        "signup_message_id": 123,
        "locked": locked,
        "signups": {} if standby else {"MT": player},
        "standby": {"42": player | {"roles": ["Tank"]}} if standby else {},
    }


def interaction():
    return SimpleNamespace(
        user=SimpleNamespace(id=42),
        channel=object(),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


class MountStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_mount_button_remains_available_when_roster_is_locked(self):
        view = bot.RaidSignupView(make_raid(locked=True))
        button = next(item for item in view.children if item.label == "Mount Status")

        self.assertTrue(view.is_persistent())
        self.assertFalse(button.disabled)
        self.assertEqual(button.row, 4)

    async def test_main_and_standby_players_can_reopen_an_answered_picker(self):
        for standby in (False, True):
            with self.subTest(standby=standby):
                raid = make_raid(standby=standby, needs_mount=False)
                event = interaction()

                async def fetch(_, event=event, raid=raid):
                    event.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
                    return raid

                with patch.object(bot, "fetch_signup", new=fetch):
                    await bot.RaidSignupView(raid).mount_callback(event)

                sent = event.followup.send.await_args
                self.assertTrue(sent.kwargs["ephemeral"])
                picker = sent.kwargs["view"]
                self.assertIsInstance(picker, bot.MountNeedView)
                self.assertEqual((picker.signup_message_id, picker.user_id), (123, 42))

    async def test_deleted_rosters_and_unregistered_players_do_not_get_a_picker(self):
        for saved_raid, message in (
            (None, "could not find"),
            ({"signups": {}, "standby": {}}, "not on this roster"),
        ):
            with self.subTest(message=message):
                event = interaction()
                with patch.object(bot, "fetch_signup", new=AsyncMock(return_value=saved_raid)):
                    await bot.RaidSignupView(make_raid()).mount_callback(event)

                sent = event.followup.send.await_args
                self.assertIn(message, sent.args[0])
                self.assertNotIn("view", sent.kwargs)

    async def test_reopened_picker_corrects_an_answer_on_a_locked_roster(self):
        raid = make_raid(locked=True, needs_mount=True)
        event = interaction()
        with (
            patch.object(bot, "fetch_signup", new=AsyncMock(return_value=raid)),
            patch.object(bot, "save_raid_state", new=AsyncMock()) as save,
            patch.object(bot, "update_raid_messages", new=AsyncMock()),
        ):
            await bot.RaidSignupView(raid).mount_callback(event)
            picker = event.followup.send.await_args.kwargs["view"]
            await picker.set_mount_status(interaction(), False)

        self.assertFalse(raid["signups"]["MT"]["needs_mount"])
        save.assert_awaited_once_with(raid)

    async def test_pending_cleanup_blocks_new_and_stale_mount_pickers(self):
        raid = make_raid(locked=True, needs_mount=True) | {"cleanup_pending": True}
        event = interaction()
        with (
            patch.object(bot, "fetch_signup", new=AsyncMock(return_value=raid)),
            patch.object(bot, "save_raid_state", new=AsyncMock()) as save,
            patch.object(bot, "update_raid_messages", new=AsyncMock()) as refresh,
        ):
            await bot.RaidSignupView(raid).mount_callback(event)
            self.assertNotIn("view", event.followup.send.await_args.kwargs)
            picker = bot.MountNeedView(123, 42)
            try:
                await picker.set_mount_status(event, False)
            finally:
                picker.stop()

        self.assertTrue(raid["signups"]["MT"]["needs_mount"])
        save.assert_not_awaited()
        refresh.assert_not_awaited()
