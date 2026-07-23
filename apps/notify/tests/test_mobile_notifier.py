# tests/test_mobile_notifier.py - Unit tests for MobileNotifier category-scoped
# home-broadcasts (2026-07-23 household push tiers).
# Run from repo root: python3 -m unittest discover -s apps/notify/tests
# mobile_notifier imports cleanly standalone once appdaemon.plugins.hass.hassapi is stubbed -
# same trick used by apps/appliances/tests/test_washer_completion.py (appdaemon isn't
# installed in the test env).

import asyncio
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if "appdaemon.plugins.hass.hassapi" not in sys.modules:
    ad = types.ModuleType("appdaemon")
    plugins = types.ModuleType("appdaemon.plugins")
    hassmod = types.ModuleType("appdaemon.plugins.hass")
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
    hassapi.Hass = object
    sys.modules["appdaemon"] = ad
    sys.modules["appdaemon.plugins"] = plugins
    sys.modules["appdaemon.plugins.hass"] = hassmod
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi

import mobile_notifier as mn  # noqa: E402


class TestFilterPeopleForCategory(unittest.TestCase):
    """Tests for the real MobileNotifier._filter_people_for_category - a pure
    staticmethod, so no app instance is needed."""

    def test_no_category_unchanged(self):
        """No category -> everyone, even with an audience map configured."""
        people = ["mikkel", "kristine", "claudia"]
        result = mn.MobileNotifier._filter_people_for_category(people, None, {"ac": ["mikkel"]})
        self.assertEqual(result, people)

    def test_category_with_audience_filters_to_intersection(self):
        """A category with an audience list restricts to that list."""
        people = ["mikkel", "kristine", "claudia"]
        result = mn.MobileNotifier._filter_people_for_category(people, "ac", {"ac": ["mikkel"]})
        self.assertEqual(result, ["mikkel"])

    def test_category_not_in_map_unchanged(self):
        """A category with no audience_category entry -> everyone (unlisted = unrestricted)."""
        people = ["mikkel", "kristine", "claudia"]
        result = mn.MobileNotifier._filter_people_for_category(people, "weather", {"ac": ["mikkel"]})
        self.assertEqual(result, people)

    def test_empty_list_audience_means_nobody(self):
        """An explicit empty-list audience filters everyone out (distinct from unlisted)."""
        people = ["mikkel", "kristine", "claudia"]
        result = mn.MobileNotifier._filter_people_for_category(people, "ac", {"ac": []})
        self.assertEqual(result, [])

    def test_none_category_audience_map_unchanged(self):
        """category_audience itself missing/None -> everyone, never an error."""
        people = ["mikkel", "kristine", "claudia"]
        result = mn.MobileNotifier._filter_people_for_category(people, "ac", None)
        self.assertEqual(result, people)


class TestGetNotificationServicesForPeople(unittest.TestCase):
    """Async test for the real get_notification_services_for_people: resolves each
    person's device_mapping entry and dedupes while preserving order."""

    def test_resolves_and_dedupes(self):
        app = mn.MobileNotifier.__new__(mn.MobileNotifier)
        app.device_mapping = {
            "mikkel": ["notify.mobile_app_mikkels_ofx9p"],
            "claudia": [
                "notify.mobile_app_claudias_iphone",
                "notify.mobile_app_mikkels_ofx9p",  # duplicate of mikkel's service
            ],
        }
        result = asyncio.run(app.get_notification_services_for_people(["mikkel", "claudia"]))
        self.assertEqual(
            result,
            ["notify.mobile_app_mikkels_ofx9p", "notify.mobile_app_claudias_iphone"],
        )


if __name__ == "__main__":
    unittest.main()
