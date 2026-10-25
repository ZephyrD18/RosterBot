import json
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

import bot


class HelperTests(unittest.TestCase):
    def test_decoders_tolerate_malformed_saved_values(self):
        for raw in (None, "", "{", "[]", "null", 42):
            with self.subTest(raw=raw):
                self.assertEqual(bot.decode_signups(raw), {})
                self.assertEqual(bot.decode_standby(raw), {})

        for roles in (None, 42, "Tank"):
            with self.subTest(roles=roles):
                raw = json.dumps({"42": {"roles": roles}})
                self.assertEqual(bot.decode_standby(raw), {})

    def test_saved_players_have_consistent_ids_and_types(self):
        signups = bot.decode_signups(json.dumps({
            "MT": {"user_id": "42", "display_name": None, "mention": [], "needs_mount": "false"},
            "invalid": {"user_id": 99},
        }))
        self.assertEqual(signups, {"MT": {
            "user_id": 42, "display_name": "Unknown", "mention": "<@42>", "needs_mount": None,
            "job": None, "ready_status": None,
        }})
        standby = bot.decode_standby(json.dumps({
            "42": {"user_id": 99, "roles": ["Tank", "Tank", "Healer", "invalid"]},
        }))
        self.assertEqual(standby["42"]["user_id"], 42)
        self.assertEqual(standby["42"]["roles"], ["Tank", "Healer"])
        self.assertEqual(bot.decode_signups("{'MT': 'Legacy player'}")["MT"]["mention"], "Legacy player")

    def test_reminder_flags_require_saved_booleans(self):
        self.assertEqual(
            bot.decode_reminders('{"automatic": "false", "five_minute": true, "other": 1}'),
            {"automatic": False, "five_minute": True, "other": False},
        )

    def test_embed_field_fits_long_lines_and_empty_fallback(self):
        self.assertEqual(bot.fit_embed_lines(["x" * 2000], "Empty"), "... and 1 more.")
        self.assertEqual(len(bot.fit_embed_lines([], "x" * 2000)), 1024)
        for count in (1, 10, 1000):
            with self.subTest(count=count):
                field = bot.fit_embed_lines(["x" * 100] * count, "Empty")
                self.assertLessEqual(len(field), 1024)

    def test_long_legacy_roster_stays_within_embed_limits(self):
        raid = {
            "signup_message_id": 123, "raid_name": "Recollection EX", "scheduled_at": 1789275600, "locked": False,
            "signups": bot.decode_signups(json.dumps({key: "x" * 4000 for key in bot.ROLE_LABELS})),
            "standby": {"42": {
                "mention": "x" * 4000, "roles": ["Tank"], "needs_mount": None,
            }},
        }
        embed = bot.roster_embed(raid)
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))
        self.assertLessEqual(len(embed), 6000)

    def test_chunks_split_overlong_lines(self):
        text = "x" * 4000
        chunks = bot.chunk_text_lines(text)
        self.assertTrue(all(0 < len(chunk) <= 1900 for chunk in chunks))
        self.assertEqual("".join(chunks), text)
        with self.assertRaises(ValueError):
            bot.chunk_text_lines(text, 0)

    def test_yearless_date_uses_raid_timezone_at_year_boundary(self):
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2027, 1, 1, 1, tzinfo=timezone.utc).astimezone(tz)

        with patch.object(bot, "datetime", FrozenDatetime):
            self.assertEqual(bot.parse_raid_date("12/31", "PT"), date(2026, 12, 31))
            self.assertEqual(bot.parse_raid_date("12/31", "UTC"), date(2027, 12, 31))

    def test_yearless_dates_never_roll_forward_to_another_year(self):
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).astimezone(tz)

        with patch.object(bot, "datetime", FrozenDatetime):
            for zone in ("PT", "ET", "UTC"):
                for value in ("10/24", "Oct 24", "October 24"):
                    with self.subTest(zone=zone, value=value):
                        self.assertEqual(bot.parse_raid_date(value, zone), date(2026, 10, 24))
            self.assertEqual(bot.parse_raid_date("10/23"), date(2026, 10, 23))
            self.assertEqual(bot.parse_raid_date("1/1"), date(2026, 1, 1))
            self.assertEqual(bot.parse_raid_date("1/1/2027"), date(2027, 1, 1))
            self.assertEqual(bot.parse_raid_date(" TODAY "), date(2026, 10, 24))
            self.assertEqual(bot.parse_raid_date("today", "UTC"), date(2026, 10, 25))
            with self.assertRaisesRegex(ValueError, "does not exist in 2026"):
                bot.parse_raid_date("2/29")
            self.assertEqual(bot.parse_raid_date("2028-02-29"), date(2028, 2, 29))

    def test_yearless_leap_day_uses_current_year(self):
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2028, 1, 1, 12, tzinfo=timezone.utc).astimezone(tz)

        with patch.object(bot, "datetime", FrozenDatetime):
            self.assertEqual(bot.parse_raid_date("2/29"), date(2028, 2, 29))
            self.assertEqual(bot.parse_raid_date("Feb 29"), date(2028, 2, 29))

    def test_dst_gap_and_repeated_hour_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            bot.parse_raid_datetime("3/8/2026", "2:30AM", "PT")
        with self.assertRaisesRegex(ValueError, "occurs twice"):
            bot.parse_raid_datetime("11/1/2026", "1:30AM", "PT")

    def test_dst_transition_boundaries_and_utc(self):
        cases = (
            ("3/8/2026", "1:59AM", "PT", datetime(2026, 3, 8, 9, 59, tzinfo=timezone.utc)),
            ("3/8/2026", "3AM", "PT", datetime(2026, 3, 8, 10, tzinfo=timezone.utc)),
            ("11/1/2026", "2AM", "PT", datetime(2026, 11, 1, 10, tzinfo=timezone.utc)),
            ("3/8/2026", "2:30AM", "UTC", datetime(2026, 3, 8, 2, 30, tzinfo=timezone.utc)),
        )
        for raid_date, raid_time, zone, expected in cases:
            with self.subTest(date=raid_date, time=raid_time, zone=zone):
                self.assertEqual(bot.parse_raid_datetime(raid_date, raid_time, zone), int(expected.timestamp()))
