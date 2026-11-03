import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import bot


def interaction_error(code=10062):
    return bot.discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"),
        {"code": code, "message": "Unknown interaction" if code == 10062 else "Unknown Message"},
    )


class InteractionErrorTests(unittest.IsolatedAsyncioTestCase):
    def event(self, *, responded=False):
        return SimpleNamespace(
            command=SimpleNamespace(name="raidsetup"),
            response=SimpleNamespace(is_done=Mock(return_value=responded), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_expired_command_logs_one_warning_without_replying(self):
        event = self.event()
        with self.assertLogs("rosterbot", level="WARNING") as logs:
            await bot.on_app_command_error(event, interaction_error())
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelno, logging.WARNING)
        self.assertIsNone(logs.records[0].exc_info)
        self.assertIn("raidsetup", logs.output[0])
        event.response.send_message.assert_not_awaited()
        event.followup.send.assert_not_awaited()

    async def test_wrapped_expired_command_does_not_retry_the_dead_token(self):
        event = self.event(responded=True)
        error = bot.app_commands.CommandInvokeError(bot.raidsetup, interaction_error())
        with self.assertLogs("rosterbot", level="WARNING") as logs:
            await bot.on_app_command_error(event, error)
        self.assertEqual(len(logs.records), 1)
        event.response.send_message.assert_not_awaited()
        event.followup.send.assert_not_awaited()

    async def test_expired_component_logs_one_warning_without_replying(self):
        event = self.event()
        view = bot.ErrorHandledView()
        item = bot.discord.ui.Button(label="DPS", custom_id="standby:123:42:DPS")
        with self.assertLogs("rosterbot", level="WARNING") as logs:
            await view.on_error(event, interaction_error(), item)
        self.assertEqual(len(logs.records), 1)
        self.assertIsNone(logs.records[0].exc_info)
        event.response.send_message.assert_not_awaited()
        event.followup.send.assert_not_awaited()
        view.stop()

    async def test_other_not_found_errors_keep_the_original_diagnostic_and_reply(self):
        event = self.event()
        with self.assertLogs("rosterbot", level="ERROR") as logs:
            await bot.on_app_command_error(event, interaction_error(10008))
        self.assertEqual(len(logs.records), 1)
        self.assertIsNotNone(logs.records[0].exc_info)
        event.response.send_message.assert_awaited_once()

    async def test_command_report_failure_never_adds_another_error_traceback(self):
        for responded in (False, True):
            for report_error in (interaction_error(), RuntimeError("Report failed")):
                with self.subTest(responded=responded, report_error=type(report_error).__name__):
                    event = self.event(responded=responded)
                    reporter = event.followup.send if responded else event.response.send_message
                    reporter.side_effect = report_error
                    with self.assertLogs("rosterbot", level="WARNING") as logs:
                        await bot.on_app_command_error(event, ValueError("Original failure"))
                    self.assertEqual([record.levelno for record in logs.records], [logging.ERROR, logging.WARNING])
                    self.assertIsNotNone(logs.records[0].exc_info)
                    self.assertIsNone(logs.records[1].exc_info)
                    reporter.assert_awaited_once()

    async def test_component_report_failure_preserves_only_the_original_traceback(self):
        event = self.event(responded=True)
        event.followup.send.side_effect = RuntimeError("Report failed")
        view = bot.ErrorHandledView()
        item = bot.discord.ui.Button(label="Standby", custom_id="raid_signup:123:standby")
        with self.assertLogs("rosterbot", level="WARNING") as logs:
            await view.on_error(event, ValueError("Original failure"), item)
        self.assertEqual([record.levelno for record in logs.records], [logging.ERROR, logging.WARNING])
        self.assertIsNone(logs.records[1].exc_info)
        view.stop()

    async def test_report_failure_warning_includes_the_reporting_error(self):
        command_event = self.event(responded=True)
        command_event.followup.send.side_effect = RuntimeError("Report failed")
        component_event = self.event(responded=True)
        component_event.followup.send.side_effect = RuntimeError("Report failed")
        view = bot.ErrorHandledView()
        item = bot.discord.ui.Button(label="Standby", custom_id="raid_signup:123:standby")
        with self.assertLogs("rosterbot", level="WARNING") as logs:
            await bot.on_app_command_error(command_event, ValueError("Original failure"))
            await view.on_error(component_event, ValueError("Original failure"), item)
        warnings = [record.getMessage() for record in logs.records if record.levelno == logging.WARNING]
        self.assertEqual(len(warnings), 2)
        self.assertTrue(all("Report failed" in warning for warning in warnings))
        view.stop()

    async def test_permission_error_still_has_an_actionable_response(self):
        event = self.event()
        error = bot.discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing permissions")
        with self.assertLogs("rosterbot", level="ERROR"):
            await bot.on_app_command_error(event, error)
        self.assertIn("missing a Discord permission", event.response.send_message.await_args.args[0])
