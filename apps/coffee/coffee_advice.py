"""The bean advisor: one trial grind change at a time, from the rated brews of the current beans. Pure.

It asks HA's AI task entity once the current beans have MIN_RATED rated complete brews of the usual,
no advice is waiting for an answer or queued, and a rating has arrived since the last ask. The answer
is a structure: action (try or none), to (a grind size), confidence and why. Only a grind change of
at most MAX_STEPS within 1-80 is taken. The trial runs once: suggested, accepted (trial_queued: the
next start of the usual sends that grind), brewed (trial_brewed), then kept (the recipe is changed)
or reverted. Dismissed lasts until the next rating. A reply in words joins the thread and asks again.
With no AI task entity configured the state is no_agent and nothing is ever asked.
"""

import json

MIN_RATED = 4
MAX_STEPS = 4
GRIND_MIN, GRIND_MAX = 1, 80
WHY_MAX = 400
REPLY_MAX = 500
THREAD_MAX = 6
EVIDENCE_MAX = 12
WAITING = ("thinking", "suggested", "trial_queued", "trial_brewed")
STATES = ("no_agent", "none", "thinking", "suggested", "trial_queued", "trial_brewed", "dismissed", "error")
TASTES = ("sour", "balanced", "bitter")

STRUCTURE = {
    "action": {"description": "try: suggest one grind change to brew once; none: keep the usual as it is",
               "required": True, "selector": {"select": {"options": ["try", "none"]}}},
    "to": {"description": "the grind size to try once (1-80, smaller is finer); only with action try",
           "required": False, "selector": {"number": {"min": GRIND_MIN, "max": GRIND_MAX, "step": 1, "mode": "box"}}},
    "confidence": {"description": "how sure the evidence makes you", "required": True,
                   "selector": {"select": {"options": ["low", "medium", "high"]}}},
    "why": {"description": "one or two plain sentences for the household, no formatting", "required": True,
            "selector": {"text": {"multiline": True}}},
}

INSTRUCTIONS = (
    "You help a household dial in the grind of their usual pour-over recipe on an xBloom Studio coffee "
    "machine, for the beans they have now. The evidence below is their rated, complete brews of that recipe "
    "with these beans: grind size (1-80, smaller is finer), brew time, drawdown, cup weight and their taste "
    "rating. Sour usually means under-extracted (go finer), bitter over-extracted (go coarser), balanced means "
    "leave it. Propose at most one change of the grind, of at most {steps} steps, to try once, or none. Give "
    "your confidence. Write why in one or two short plain sentences, no formatting, at most {why} characters. "
    "If the household replied, take their words into account.\n\nEvidence:\n{evidence}"
)


def clean(text, limit):
    text = " ".join(str(text or "").encode("ascii", "ignore").decode("ascii").split())
    return text[:limit].strip()


def rated_evidence(records, bean_id, recipe_key):
    """The rated complete brews of the usual with the current beans, oldest first."""
    found = [r for r in records or [] if r.get("outcome") == "complete" and r.get("variant") in ("usual", "base")
             and r.get("rating") in TASTES and (r.get("bean") or {}).get("id") == bean_id
             and (r.get("recipe") or {}).get("key") == recipe_key]
    found.sort(key=lambda r: r.get("started_at") or "")
    return found


class Advice:
    def __init__(self, data=None, enabled=True):
        self.enabled = enabled
        self.data = dict(data) if isinstance(data, dict) else {"state": "none"}
        if self.data.get("state") not in STATES or self.data.get("state") == "no_agent":
            self.data["state"] = "none"

    def to_dict(self):
        return self.data

    @property
    def state(self):
        return self.data["state"] if self.enabled else "no_agent"

    def view(self, bean_id):
        """What sensor.coffee_advice shows (numbers and lists left as they are)."""
        if not self.enabled:
            return {"state": "no_agent"}
        if self.data.get("bean_id") and self.data.get("bean_id") != bean_id:
            return {"state": "none"}
        keys = ("advice_id", "bean_id", "field", "from", "to", "why", "confidence", "brew_ids", "trial_brew_id",
                "thread", "created_at", "error")
        shown = {k: self.data[k] for k in keys if self.data.get(k) not in (None, "", [])}
        return {"state": self.data["state"], **shown}

    def _reset(self, state="none", **keep):
        self.data = {"state": state, **keep}

    def bean_changed(self, bean_id):
        if self.data.get("bean_id") and self.data.get("bean_id") != bean_id:
            self._reset("none", asked_with=self.data.get("asked_with"))

    def should_ask(self, evidence):
        if not self.enabled or len(evidence) < MIN_RATED or self.data["state"] in WAITING:
            return False
        rated = sorted(f"{r['id']}:{r['rating']}" for r in evidence)
        return rated != sorted(self.data.get("asked_with") or [])

    def asking(self, evidence, recipe, bean, baseline, now_iso, grind):
        """Marks the ask and returns the generate_data request."""
        thread = list(self.data.get("thread") or []) if self.data["state"] == "thinking" else []
        self.data.update(state="thinking", bean_id=bean.get("id"), field="grind_size", grind_now=grind,
                         asked_with=[f"{r['id']}:{r['rating']}" for r in evidence],
                         brew_ids=[r["id"] for r in evidence][-EVIDENCE_MAX:], thread=thread, error=None)
        brews = []
        for r in evidence[-EVIDENCE_MAX:]:
            brews.append({k: r.get(k) for k in ("id", "started_at", "total_s", "drawdown_s", "cup_g", "water_ml",
                                                "rating", "in_band") if r.get(k) is not None}
                         | {"grind": (r.get("recipe") or {}).get("grind")})
        evidence_doc = {"beans": bean.get("name"), "recipe": {k: recipe.get(k) for k in ("name", "dose_g", "ratio",
                                                                                         "grind", "rpm")},
                        "usual_now": baseline, "brews": brews, "thread": thread}
        return {"task_name": "coffee grind advice",
                "instructions": INSTRUCTIONS.format(steps=MAX_STEPS, why=WHY_MAX,
                                                    evidence=json.dumps(evidence_doc, sort_keys=True)),
                "structure": STRUCTURE}

    def answered(self, response, now_iso, advice_id):
        """The AI task's answer (its `data`). Returns the new state."""
        data = response if isinstance(response, dict) else {}
        grind = self.data.get("grind_now")
        why = clean(data.get("why"), WHY_MAX)
        confidence = data.get("confidence") if data.get("confidence") in ("low", "medium", "high") else "low"
        try:
            to = int(round(float(data.get("to"))))
        except (TypeError, ValueError):
            to = None
        thread = list(self.data.get("thread") or [])
        if why:
            thread.append({"role": "advisor", "who": "advisor", "at": now_iso, "text": why})
        common = {"advice_id": advice_id, "why": why, "confidence": confidence, "created_at": now_iso,
                  "thread": thread[-THREAD_MAX:]}
        if (data.get("action") == "try" and isinstance(grind, int) and to is not None and to != grind
                and GRIND_MIN <= to <= GRIND_MAX and abs(to - grind) <= MAX_STEPS):
            self.data.update(state="suggested", **{"from": grind, "to": to}, **common)
        else:
            self.data.update(state="none", **common)
            self.data.pop("to", None)
        return self.data["state"]

    def failed(self, error):
        self.data.update(state="error", error=clean(error, 120) or "no answer")

    def _is(self, advice_id, *states):
        return self.enabled and advice_id == self.data.get("advice_id") and self.data["state"] in states

    def accept(self, advice_id):
        if self._is(advice_id, "suggested"):
            self.data["state"] = "trial_queued"
            return True
        return False

    def undo(self, advice_id):
        if self._is(advice_id, "trial_queued"):
            self.data.update(state="suggested", trial_brew_id=None)
            return True
        return False

    def dismiss(self, advice_id):
        if self._is(advice_id, "suggested", "none", "error", "trial_queued"):
            self.data["state"] = "dismissed"
            return True
        return False

    def reply(self, advice_id, text, who, now_iso):
        text = clean(text, REPLY_MAX)
        if not text or not self._is(advice_id, "suggested", "none", "error", "dismissed", "trial_brewed"):
            return False
        thread = (list(self.data.get("thread") or []) + [{"role": "person", "who": who or "none", "at": now_iso,
                                                         "text": text}])[-THREAD_MAX:]
        self.data.update(state="thinking", thread=thread)
        return True

    def trial_grind(self, variant):
        """The grind a start of `variant` sends for the queued trial, else None."""
        if variant == "usual" and self.enabled and self.data["state"] == "trial_queued":
            return self.data.get("to")
        return None

    def trial_text(self):
        return f"grind_size:{self.data.get('from')}>{self.data.get('to')}"

    def trial_started(self, brew_id):
        self.data["trial_brew_id"] = brew_id

    def trial_finished(self, brew_id, complete):
        if self.data.get("trial_brew_id") != brew_id or self.data["state"] != "trial_queued":
            return
        if complete:
            self.data["state"] = "trial_brewed"
        else:
            self.data["trial_brew_id"] = None

    def keep(self, advice_id):
        """The grind to save into the usual, or None."""
        return self.data.get("to") if self._is(advice_id, "trial_brewed") else None

    def kept(self):
        self._reset("none", asked_with=self.data.get("asked_with"))

    def keep_failed(self, error):
        self.data["error"] = clean(error, 120) or "update failed"

    def revert(self, advice_id):
        if self._is(advice_id, "trial_brewed"):
            self._reset("none", asked_with=self.data.get("asked_with"))
            return True
        return False
