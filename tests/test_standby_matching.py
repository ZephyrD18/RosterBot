import json
import unittest

import bot


class StandbyMatchingTests(unittest.TestCase):
    def raid(self, *open_slots):
        return {
            "signups": {
                role_key: {"user_id": index + 100}
                for index, (role_key, *_) in enumerate(bot.ROLES) if role_key not in open_slots
            },
            "standby": {},
        }

    def player(self, user_id, *roles):
        return {"user_id": user_id, "mention": f"<@{user_id}>", "roles": list(roles)}

    def matches(self, raid):
        return [player["user_id"] for player in bot.matching_standby_players(raid)]

    def test_tank_and_melee_flex_player_matches_an_open_tank_slot(self):
        raid = self.raid("MT")
        raid["standby"] = {
            "42": self.player(42, "Tank", "Melee DPS"),
            "43": self.player(43, "Range DPS"),
        }
        self.assertEqual(self.matches(raid), [42])

    def test_each_slot_matches_its_standby_category(self):
        expected = {
            "MT": "Tank", "OT": "Tank", "H1": "Healer", "H2": "Healer",
            "M1": "Melee DPS", "M2": "Melee DPS", "R1": "Range DPS", "R2": "Range DPS",
        }
        for role_key, category in expected.items():
            with self.subTest(role_key=role_key):
                raid = self.raid(role_key)
                raid["standby"] = {
                    str(index): self.player(index, standby_category)
                    for index, standby_category in enumerate(bot.STANDBY_CHOICES, start=42)
                }
                self.assertEqual(self.matches(raid), [42 + bot.STANDBY_CHOICES.index(category)])

    def test_full_roster_never_matches_standby_players(self):
        raid = self.raid()
        raid["standby"] = {"42": self.player(42, *bot.STANDBY_CHOICES)}
        self.assertEqual(self.matches(raid), [])

    def test_closed_rosters_do_not_recruit_standby_into_unclaimable_slots(self):
        for closed_state in ({"locked": True}, {"cleanup_pending": True}, {"finished_at": 1234}):
            with self.subTest(closed_state=closed_state):
                raid = self.raid("MT") | closed_state
                raid["standby"] = {"42": self.player(42, "Tank")}
                self.assertEqual(self.matches(raid), [])

    def test_multiple_open_slots_ping_a_matching_player_only_once(self):
        raid = self.raid("MT", "OT", "M1")
        raid["standby"] = {
            "42": self.player(42, "Tank", "Melee DPS"),
            "legacy_duplicate": self.player(42, "Tank"),
        }
        self.assertEqual(self.matches(raid), [42])

    def test_main_roster_members_and_invalid_ids_are_excluded(self):
        raid = self.raid("MT")
        raid["signups"]["M1"] = {"user_id": 42}
        raid["standby"] = {
            "42": self.player(42, "Tank"),
            "invalid": self.player("43", "Tank"),
            "boolean": self.player(True, "Tank"),
        }
        self.assertEqual(self.matches(raid), [])

    def test_legacy_dps_expands_to_both_new_dps_categories(self):
        saved = bot.decode_standby(json.dumps({
            "42": {"roles": ["Tank", "DPS"], "job": "Warrior", "needs_mount": False},
        }))
        self.assertEqual(saved["42"]["roles"], ["Tank", "Melee DPS", "Range DPS"])
        self.assertEqual(saved["42"]["job"], "Warrior")
        self.assertFalse(saved["42"]["needs_mount"])
        raid = self.raid("R2") | {"standby": saved}
        self.assertEqual(self.matches(raid), [42])

    def test_roles_are_ordered_deduplicated_and_unknown_categories_removed(self):
        roles = ["Range DPS", "Tank", "Tank", "Unknown", "Healer", "Melee DPS"]
        self.assertEqual(bot.normalize_standby_roles(roles), bot.STANDBY_CHOICES)
        self.assertEqual(bot.standby_role_text(roles), "Any")
        self.assertEqual(bot.standby_role_text(["Melee DPS", "Tank"]), "Tank/Melee DPS")
