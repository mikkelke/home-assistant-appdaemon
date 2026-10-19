# tests/test_coffee_advice.py - the advisor with a stubbed AI task answer: when it asks, which answers it
# takes, the one-trial lifecycle, replies, dismissal until the next rating, and no_agent.
# Run from repo root: python3 -m unittest discover -s apps/coffee/tests

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coffee_advice as ca  # noqa: E402

KEY = "k1"
RECIPE = {"name": "4 Pour Recipe", "dose_g": 15.0, "ratio": 17.0, "grind": 46, "rpm": 90}
BEAN = {"id": "b1", "name": "Kenya Kiambu"}


def rated(i, taste, **extra):
    return {"id": f"c{1000 + i}", "started_at": f"2026-10-{10 + i:02d}T06:00:00+00:00", "outcome": "complete",
            "variant": "usual", "rating": taste, "bean": {"id": "b1"}, "recipe": {"key": KEY, "grind": 46},
            "total_s": 205, "cup_g": 214.0, **extra}


FOUR = [rated(1, "sour"), rated(2, "sour"), rated(3, "balanced"), rated(4, "sour")]


def ask(advice, evidence=FOUR):
    return advice.asking(evidence, RECIPE, BEAN, {"n": 4}, "2026-10-20T06:00:00+00:00", 46)


class EvidenceTests(unittest.TestCase):
    def test_only_rated_complete_usual_brews_of_these_beans_and_recipe(self):
        records = FOUR + [rated(5, "sour", outcome="stopped"), rated(6, "sour", variant="trial"),
                          rated(7, "bitter", bean={"id": "b2"}), rated(8, "meh"), rated(9, "sour", recipe={"key": "x"}),
                          rated(10, "bitter", variant="base")]
        self.assertEqual([r["id"] for r in ca.rated_evidence(records, "b1", KEY)],
                         ["c1001", "c1002", "c1003", "c1004", "c1010"])


class AskTests(unittest.TestCase):
    def test_asks_from_four_rated_brews_and_not_again_until_a_new_rating(self):
        advice = ca.Advice()
        self.assertFalse(advice.should_ask(FOUR[:3]))
        self.assertTrue(advice.should_ask(FOUR))
        request = ask(advice)
        self.assertEqual(advice.state, "thinking")
        self.assertFalse(advice.should_ask(FOUR))
        advice.answered({"action": "none", "confidence": "low", "why": "Keep it."}, "t", "a1")
        self.assertFalse(advice.should_ask(FOUR))
        self.assertTrue(advice.should_ask(FOUR + [rated(5, "balanced")]))
        self.assertEqual(set(request), {"task_name", "instructions", "structure"})
        self.assertEqual(set(request["structure"]), {"action", "to", "confidence", "why"})
        evidence = json.loads(request["instructions"].split("Evidence:\n", 1)[1])
        self.assertEqual([b["rating"] for b in evidence["brews"]], ["sour", "sour", "balanced", "sour"])
        self.assertEqual(evidence["beans"], "Kenya Kiambu")
        self.assertTrue(request["instructions"].isascii())

    def test_no_agent_never_asks_and_takes_no_op(self):
        advice = ca.Advice(enabled=False)
        self.assertEqual(advice.state, "no_agent")
        self.assertEqual(advice.view("b1"), {"state": "no_agent"})
        self.assertFalse(advice.should_ask(FOUR))
        self.assertFalse(advice.accept("a1"))
        self.assertIsNone(advice.trial_grind("usual"))


class AnswerTests(unittest.TestCase):
    def setUp(self):
        self.advice = ca.Advice()
        ask(self.advice)

    def test_a_grind_change_within_four_steps_is_suggested(self):
        state = self.advice.answered({"action": "try", "to": 44, "confidence": "medium",
                                      "why": "Three of four were sour: go two steps finer."}, "t1", "a1")
        self.assertEqual(state, "suggested")
        view = self.advice.view("b1")
        self.assertEqual((view["from"], view["to"], view["confidence"], view["advice_id"]), (46, 44, "medium", "a1"))
        self.assertEqual(view["brew_ids"], ["c1001", "c1002", "c1003", "c1004"])
        self.assertEqual(view["thread"][-1]["role"], "advisor")

    def test_out_of_range_or_too_big_changes_are_not_taken(self):
        for answer in ({"action": "try", "to": 40}, {"action": "try", "to": 46}, {"action": "try", "to": 0},
                       {"action": "try", "to": "x"}, {"action": "try"}, {"action": "maybe", "to": 44}, None):
            advice = ca.Advice()
            ask(advice)
            self.assertEqual(advice.answered(answer, "t", "a1"), "none", answer)
            self.assertNotIn("to", advice.view("b1"))

    def test_why_is_short_plain_ascii(self):
        self.advice.answered({"action": "try", "to": 44, "confidence": "high", "why": "Sour — go finer. " * 60},
                             "t", "a1")
        why = self.advice.view("b1")["why"]
        self.assertLessEqual(len(why), ca.WHY_MAX)
        self.assertTrue(why.isascii())

    def test_a_failure_is_an_error_and_the_next_rating_retries(self):
        self.advice.failed("TimeoutError()")
        self.assertEqual(self.advice.state, "error")
        self.assertFalse(self.advice.should_ask(FOUR))
        self.assertTrue(self.advice.should_ask(FOUR + [rated(5, "sour")]))


class TrialTests(unittest.TestCase):
    def suggested(self):
        advice = ca.Advice()
        ask(advice)
        advice.answered({"action": "try", "to": 44, "confidence": "medium", "why": "Go finer."}, "t", "a1")
        return advice

    def test_the_whole_trial_and_keep(self):
        advice = self.suggested()
        self.assertIsNone(advice.trial_grind("usual"))
        self.assertTrue(advice.accept("a1"))
        self.assertEqual(advice.state, "trial_queued")
        self.assertIsNone(advice.trial_grind("base"))
        self.assertIsNone(advice.trial_grind("tweak"))
        self.assertEqual(advice.trial_grind("usual"), 44)
        self.assertEqual(advice.trial_text(), "grind_size:46>44")
        advice.trial_started("c2000")
        advice.trial_finished("c2000", complete=True)
        self.assertEqual(advice.state, "trial_brewed")
        self.assertEqual(advice.keep("a1"), 44)
        advice.kept()
        self.assertEqual(advice.state, "none")
        self.assertFalse(advice.should_ask(FOUR))

    def test_a_trial_that_did_not_finish_stays_queued(self):
        advice = self.suggested()
        advice.accept("a1")
        advice.trial_started("c2000")
        advice.trial_finished("c2000", complete=False)
        self.assertEqual(advice.state, "trial_queued")
        self.assertEqual(advice.trial_grind("usual"), 44)

    def test_undo_revert_and_dismiss(self):
        advice = self.suggested()
        advice.accept("a1")
        self.assertTrue(advice.undo("a1"))
        self.assertEqual(advice.state, "suggested")
        advice.accept("a1")
        advice.trial_started("c2000")
        advice.trial_finished("c2000", complete=True)
        self.assertIsNone(advice.keep("a2"))
        self.assertTrue(advice.revert("a1"))
        self.assertEqual(advice.state, "none")
        advice = self.suggested()
        self.assertTrue(advice.dismiss("a1"))
        self.assertEqual(advice.state, "dismissed")
        self.assertFalse(advice.should_ask(FOUR))
        self.assertTrue(advice.should_ask(FOUR + [rated(5, "bitter")]))

    def test_ops_for_another_advice_are_ignored(self):
        advice = self.suggested()
        self.assertFalse(advice.accept("a9"))
        self.assertFalse(advice.dismiss("a9"))
        self.assertEqual(advice.state, "suggested")

    def test_a_reply_joins_the_thread_and_asks_again(self):
        advice = self.suggested()
        self.assertTrue(advice.reply("a1", "It was more bitter than sour, honestly.", "mikkel", "t2"))
        self.assertEqual(advice.state, "thinking")
        request = ask(advice)
        evidence = json.loads(request["instructions"].split("Evidence:\n", 1)[1])
        self.assertEqual([e["role"] for e in evidence["thread"]], ["advisor", "person"])
        self.assertFalse(advice.reply("a1", "", "mikkel", "t3"))

    def test_the_thread_keeps_the_last_six(self):
        advice = self.suggested()
        for i in range(8):
            advice.reply("a1", f"reply {i}", "mikkel", f"t{i}")
            ask(advice)
            advice.answered({"action": "none", "confidence": "low", "why": f"answer {i}"}, f"t{i}", "a1")
        self.assertEqual(len(advice.view("b1")["thread"]), ca.THREAD_MAX)

    def test_new_beans_end_the_advice_for_the_old_ones(self):
        advice = self.suggested()
        self.assertEqual(advice.view("b2"), {"state": "none"})
        advice.bean_changed("b2")
        self.assertEqual(advice.state, "none")

    def test_the_advice_survives_a_round_trip(self):
        advice = self.suggested()
        again = ca.Advice(json.loads(json.dumps(advice.to_dict())))
        self.assertEqual(again.view("b1"), advice.view("b1"))


if __name__ == "__main__":
    unittest.main()
