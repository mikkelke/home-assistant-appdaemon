# tests/test_coffee_connect.py - when the coffee app takes and lets go of the machine's link: edges only,
# retries while the trigger holds, the quiet-off, and a person's off.
# Run from repo root: python3 -m unittest discover -s apps/coffee/tests

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coffee_connect as cc  # noqa: E402


def holds(value=True):
    return lambda kind: value


class TriggerTable(unittest.TestCase):
    def test_triggers(self):
        # (kind, switch on, suppressed) -> action
        table = [
            ("power_rise", False, False, "on"), ("power_rise", True, False, None), ("power_rise", False, True, None),
            ("init_working", False, False, "on"), ("init_working", False, True, None),
            ("op_start", False, False, "on"), ("op_start", False, True, "on"), ("op_start", True, False, None),
            ("op_connect", False, True, "on"), ("wake", False, False, None), ("level", False, False, None),
        ]
        for kind, switch_on, suppressed, expected in table:
            policy = cc.ConnectPolicy()
            if suppressed:
                policy.person_off(0)
            self.assertEqual(policy.trigger(kind, 10, switch_on), expected, (kind, switch_on, suppressed))


class RetryTests(unittest.TestCase):
    def test_two_retries_twenty_seconds_apart_while_the_trigger_holds(self):
        policy = cc.ConnectPolicy()
        self.assertEqual(policy.trigger("power_rise", 0, False), "on")
        policy.failed(5)
        self.assertIsNone(policy.tick(24, False, False, holds()))
        self.assertEqual(policy.tick(25, False, False, holds()), "on")
        policy.failed(30)
        self.assertEqual(policy.tick(50, False, False, holds()), "on")
        policy.failed(55)
        self.assertIsNone(policy.tick(80, False, False, holds()))
        self.assertIsNone(policy.tick(200, False, False, holds()))

    def test_no_retry_once_the_trigger_is_gone(self):
        policy = cc.ConnectPolicy()
        policy.trigger("op_start", 0, False)
        policy.failed(3)
        self.assertIsNone(policy.tick(30, False, False, holds(False)))

    def test_a_ready_link_ends_the_retries(self):
        policy = cc.ConnectPolicy()
        policy.trigger("power_rise", 0, False)
        policy.connected(4)
        policy.failed(100)
        self.assertIsNone(policy.tick(130, False, False, holds()))


class OffTests(unittest.TestCase):
    def test_off_ten_minutes_after_the_last_activity_and_never_while_busy(self):
        policy = cc.ConnectPolicy()
        policy.trigger("op_connect", 0, False)
        self.assertIsNone(policy.tick(599, True, False, holds()))
        self.assertEqual(policy.tick(600, True, False, holds()), "off")
        self.assertIsNone(policy.tick(5000, True, True, holds()))
        policy.activity(5000)
        self.assertIsNone(policy.tick(5300, True, False, holds()))
        self.assertEqual(policy.tick(5600, True, False, holds()), "off")

    def test_nothing_to_turn_off_when_off(self):
        self.assertIsNone(cc.ConnectPolicy().tick(10_000, False, False, holds()))

    def test_no_reconnect_loop_while_the_machine_idles_awake(self):
        policy = cc.ConnectPolicy()
        policy.trigger("power_rise", 0, False)
        policy.connected(3)
        switch_on, actions = True, []
        for t in range(10, 4000, 10):
            action = policy.tick(t, switch_on, False, holds())
            if action:
                actions.append((t, action))
                switch_on = action == "on"
        self.assertEqual(actions, [(610, "off")])

    def test_a_person_switching_it_off_stops_automatic_connects_for_thirty_minutes(self):
        policy = cc.ConnectPolicy()
        policy.person_off(100)
        self.assertIsNone(policy.trigger("power_rise", 1000, False))
        self.assertEqual(policy.trigger("power_rise", 100 + cc.SUPPRESS_S, False), "on")

    def test_the_policy_survives_a_round_trip(self):
        policy = cc.ConnectPolicy()
        policy.person_off(100)
        policy.activity(200)
        again = cc.ConnectPolicy(policy.to_dict())
        self.assertEqual(again.to_dict(), policy.to_dict())
        self.assertTrue(again.suppressed(300))


if __name__ == "__main__":
    unittest.main()
