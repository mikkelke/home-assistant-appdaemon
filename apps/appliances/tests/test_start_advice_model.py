"""Energy Plan parity vectors from startPlan/runProfiles/appliancePlan.test.ts.

The start-advice-* JSON fixtures are unchanged copies of the dashboard test fixtures.
They keep these tests independent of a dashboard checkout.
"""
from __future__ import annotations

import importlib
import json
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import start_advice_model as m

MINUTE = 60_000
HOUR = 60 * MINUTE
FIXTURES = Path(__file__).parent / 'fixtures'
STEPS = list(range(0, 181, 30)) + list(range(240, 1441, 60))
HOURLY_STEPS = list(range(0, 1441, 60))


def home(day=9, hour=20, minute=0, second=0):
    return int(datetime(2026, 10, day, hour, minute, second,
                        tzinfo=ZoneInfo('Europe/Copenhagen')).timestamp() * 1000)


def fixture(name):
    return json.loads((FIXTURES / ('start-advice-' + name)).read_text())


def parsed(attrs):
    result = m.parse_run_profiles(attrs)
    assert result['ok'], result
    return result['profiles']['appliances']


def programme(wh, completion='length'):
    raw = {'key': 't', 'label': 'T', 'n': 5, 'p': ','.join(str(round(x * 10)) for x in wh)}
    if completion is not None:
        raw['completion_min'] = len(wh) if completion == 'length' else completion
    return parsed({'schema': 1, 'appliances': {'test': {'programmes': [raw]}}})['test'][0]


def hourly(start, values, estimated=False):
    return [dict(ms=start + i * HOUR, price=p, **({'estimated': True} if estimated else {}))
            for i, p in enumerate(values) if p is not None]


def plan(p, prices, now=None, steps=STEPS, **kwargs):
    return m.plan_start(p, prices, dict(nowMs=home() if now is None else now,
                                      knobStepsMin=steps, **kwargs))


def attrs(**overrides):
    raw = dict(key='x', label='X', n='7', energy_min='5', completion_min='9', p='0,0,100,0,200')
    raw.update(overrides)
    return {'schema': 1, 'appliances': {'washer': {'programmes': [raw]}}}


class ProfileParity(unittest.TestCase):
    def test_measured_and_published_fixtures(self):
        profiles = parsed(fixture('run-profiles.json')['attributes'])
        p = profiles['washer'][0]
        self.assertEqual((p['key'], p['energyMin'], p['completionMin']), ('bomuld|60', 188, 190))
        self.assertAlmostEqual(p['kwh'], 1.1, 2)
        self.assertTrue(p['labelConfirmed'])
        self.assertEqual(len(p['cumWh']), 189)
        self.assertFalse(profiles['dryer'][0]['labelConfirmed'])
        dw = profiles['dishwasher'][0]
        self.assertEqual(dw['whPerMin'][-5:], [0] * 5)
        self.assertEqual(dw['whPerMin'].count(0), 11)
        published = parsed(fixture('run-profiles-published.json'))
        self.assertEqual(list(published), ['washer', 'dishwasher', 'dryer'])
        self.assertEqual(published['dishwasher'][0]['key'], 'typical')
        self.assertEqual(published['dishwasher'][0]['completionMin'], 250)
        self.assertEqual(published['dishwasher'][0]['energyMin'], 101)

    def test_zeroes_aliases_completion_and_fractional_energy(self):
        p = parsed(attrs())['washer'][0]
        self.assertEqual(p['whPerMin'], [0, 0, 10, 0, 20])
        self.assertEqual(p['cumWh'], [0, 0, 0, 10, 10, 30])
        self.assertEqual(m.energy_between_wh(p, 2.5, 3), 5)
        self.assertEqual(m.energy_between_wh(p, -10, 100), 30)
        self.assertEqual(m.energy_between_wh(p, 3, 4), 0)
        for source in ('mixed', 'confirmed', 'inferred'):
            self.assertEqual(parsed(attrs(label_source=source))['washer'][0]['labelConfirmed'], source != 'inferred')
        raw = attrs()['appliances']['washer']['programmes'][0]
        del raw['completion_min']
        del raw['energy_min']
        wire = {'schema': '1', 'appliances': {'washer': {'programmes': [raw]}}}
        self.assertIsNone(parsed(wire)['washer'][0]['completionMin'])
        raw['done_min'] = '12'
        raw['run_min'] = '5'
        self.assertEqual(parsed(wire)['washer'][0]['completionMin'], 12)
        self.assertEqual(parsed(attrs(completion_min='3'))['washer'][0]['completionMin'], 5)

    def test_malformed_payload_rejects_whole_entity(self):
        for changes in ({'p': [100, 200]}, {'p': '1,-1,2'}, {'p': '1,1.5'}, {'p': ''},
                        {'p': ','.join(['1'] * 721), 'energy_min': 721}, {'energy_min': 6},
                        {'n': 0}, {'n': True}, {'label_source': 'maybe'}, {'label': ''},
                        {'completion_min': None}, {'p': '9007199254740992'}):
            with self.subTest(changes=changes):
                self.assertFalse(m.parse_run_profiles(attrs(**changes))['ok'])
        self.assertTrue(m.parse_run_profiles(attrs(p=','.join(['1'] * 720), energy_min=720))['ok'])
        wire = attrs()
        wire['appliances']['washer']['programmes'] *= 2
        self.assertFalse(m.parse_run_profiles(wire)['ok'])
        for wire in (None, {'schema': 2, 'appliances': {}}, {'schema': 1},
                     {'schema': 1, 'appliances': {'washer': {'programmes': 'x'}}}):
            self.assertFalse(m.parse_run_profiles(wire)['ok'])


class PlannerParity(unittest.TestCase):
    def assert_window(self, p, timeline, result, done_by=None):
        choice = result['plan']
        prices = {x['ms']: x for x in timeline}
        self.assertGreaterEqual(choice['pressFromMs'], result['pressFromMs'])
        self.assertLess(choice['pressUntilMs'], result['pressFromMs'] // HOUR * HOUR + HOUR)
        t = choice['pressFromMs']
        while t <= choice['pressUntilMs']:
            c = m.start_cost(p, prices, t + choice['knobMin'] * MINUTE, done_by)
            self.assertIsNotNone(c)
            self.assertLessEqual(c['kr'], choice['costKr'] + .05 + 1e-9)
            t = t // MINUTE * MINUTE + MINUTE

    def test_knob_steps(self):
        self.assertEqual(list(m.KNOB_STEPS_MIN), STEPS)
        self.assertEqual(len(STEPS), 28)

    def test_measured_acceptance_cases(self):
        profiles = parsed(fixture('run-profiles.json')['attributes'])
        data = fixture('prices-2026-10.json')
        timeline = m.build_hourly_prices(data['2026-10-01'] + data['2026-10-02'], None, False, None)
        washer = profiles['washer'][0]
        prices = {x['ms']: x for x in timeline}
        for minute, delay, expected in ((5, 0, 3.94), (50, 0, 3.18), (5, 6, 1.98), (50, 6, 1.99)):
            self.assertLessEqual(abs(m.run_cost(washer, prices, home(1, 20, minute) + delay * HOUR)['kr'] - expected), .02)
        result = plan(washer, timeline, home(1, 20, 5))
        self.assertEqual((result['plan']['kind'], result['plan']['knobMin']), ('knob', 360))
        self.assert_window(washer, timeline, result)
        dw = profiles['dishwasher'][0]
        timeline = m.build_hourly_prices(data['2026-10-09'] + data['2026-10-10'], None, False, None)
        result = plan(dw, timeline, home(minute=5))
        c = result['plan']
        self.assertEqual((c['kind'], c['knobMin'], c['pressFromMs']), ('knob', 480, home(minute=5)))
        self.assertLessEqual(abs(c['costKr'] - .33), .01)
        self.assertLessEqual(abs(c['pressUntilMs'] - home(minute=30)), MINUTE)
        self.assertTrue(c['showPressBy'])
        self.assertAlmostEqual(result['savingKr'], result['noDelay']['kr'] - c['costKr'])
        self.assert_window(dw, timeline, result)
        for minute in (0, 20, 45, 58):
            self.assert_window(washer, timeline, plan(washer, timeline, home(minute=minute, second=30)))

    def test_wait_window_and_advertised_cost(self):
        p = programme([20] * 60)
        timeline = hourly(home(), [2, 2, 2, 2, 2, 0, 2, 2, 2, 2])
        result = plan(p, timeline, home(minute=5), HOURLY_STEPS)
        c = result['plan']
        self.assertEqual((c['kind'], c['knobMin'], c['pressFromMs'], c['pressUntilMs']),
                         ('wait', 240, home(minute=58), home(minute=59)))
        self.assertAlmostEqual(c['costKr'], .08)
        p = programme([10] * 60)
        timeline = hourly(home(), [2, 2, 2.22, 2.22, 1.78, 2.22, 2.22, 2.22])
        result = plan(p, timeline, home(minute=30), HOURLY_STEPS)
        c = result['plan']
        self.assertEqual((c['kind'], c['knobMin'], c['pressFromMs']), ('wait', 180, home(minute=58)))
        self.assertAlmostEqual(c['costKr'], m.run_cost(p, {x['ms']: x for x in timeline}, c['startMs'])['kr'])
        self.assertTrue(m.passes_saving_rule(result['noDelay']['kr'], c['costKr']))
        self.assert_window(p, timeline, result)

    def test_cost_fractional_missing_zero_and_forecast_hours(self):
        p = programme([60] * 60)
        self.assertAlmostEqual(m.run_cost(p, {x['ms']: x for x in hourly(home(), [1, 2])},
                                         home(minute=59, second=30))['kr'], .03 + 3.57 * 2)
        p = programme([10] * 60 + [0] * 60 + [10] * 60)
        self.assertAlmostEqual(m.run_cost(p, {x['ms']: x for x in hourly(home(), [1, None, 1])}, home())['kr'], 1.2)
        self.assertIsNone(m.run_cost(p, {x['ms']: x for x in hourly(home(), [1, 1, None])}, home()))
        p = programme([10] * 90)
        timeline = hourly(home(), [1]) + hourly(home() + HOUR, [1], True)
        self.assertTrue(plan(p, timeline, steps=[0])['plan']['estimated'])
        self.assertIsNone(m.run_cost(p, {x['ms']: x for x in timeline}, home() - 40 * MINUTE))
        p = programme([10] * 60)
        result = plan(p, hourly(home(), [3, 3, 1]), home(minute=5), HOURLY_STEPS)
        self.assertEqual(result['pricedUntilMs'], home() + 3 * HOUR)
        self.assertEqual(result['plan']['knobMin'], 60)
        self.assertEqual(result['plan']['kind'], 'wait')
        self.assertIsNone(plan(p, [])['plan'])

    def test_saving_rule_negative_prices_and_half_hour_steps(self):
        for baseline, cost, expected in ((1, .9, True), (.5, .41, False), (2, 1.85, False),
                                         (0, -.1, True), (-1, -1.05, False), (-1, -1.1, True)):
            self.assertEqual(m.passes_saving_rule(baseline, cost), expected)
        p = programme([10] * 60)
        result = plan(p, hourly(home(), [.5, 0, -.4]), steps=HOURLY_STEPS)
        self.assertEqual(result['plan']['knobMin'], 120)
        self.assertAlmostEqual(result['savingKr'], .54)
        result = plan(p, hourly(home(), [1, .95, .95]), steps=HOURLY_STEPS)
        self.assertEqual(result['plan']['kind'], 'now')
        self.assertIsNone(result['savingKr'])
        result = plan(programme([20] * 30), hourly(home(), [2, .1, 2, 2]), home(minute=30))
        self.assertEqual((result['plan']['kind'], result['plan']['knobMin']), ('knob', 30))

    def test_completion_and_earliest_press(self):
        prices = hourly(home(), [1] * 10)
        p = programme([10] * 60)
        result = plan(p, prices, home(minute=5), [0], doneByMs=home(hour=21, minute=30))
        self.assertEqual(result['plan']['pressUntilMs'], home(minute=30))
        self.assert_window(p, prices, result, home(hour=21, minute=30))
        result = plan(p, prices, doneByMs=home(minute=30))
        self.assertTrue(result['doneByMissed'])
        self.assertIsNone(result['plan'])
        result = plan(programme([10] * 60, None), prices, doneByMs=home(minute=30))
        self.assertFalse(result['doneByMissed'])
        self.assertIsNone(result['plan']['doneMs'])
        result = plan(p, prices, earliestPressMs=home(hour=21, minute=40))
        self.assertEqual(result['pressFromMs'], home(hour=21, minute=40))


class TimelineParity(unittest.TestCase):
    def test_merge_range_alignment_and_published_precedence(self):
        def point(ms, price):
            return {'hour': datetime.fromtimestamp(ms / 1000, ZoneInfo('UTC')).isoformat(), 'price': price}
        today = [point(home() + i * HOUR, p) for i, p in enumerate([.5, 0, -.4, 109.57, -5, 15, -5.01, 15.01])]
        today += [point(home() + 1, 1), {'hour': 'bad', 'price': 1}, point(home() + 8 * HOUR, '1')]
        tomorrow = [point(home() + 9 * HOUR, 2)]
        forecast = [point(home(), .1), point(home() + 9 * HOUR, 3)]
        points = m.build_hourly_prices(today, tomorrow, True, forecast)
        self.assertEqual([x['price'] for x in points], [.5, 0, -.4, -5, 15, 2])
        self.assertFalse(any(x.get('estimated') for x in points))
        self.assertTrue(m.build_hourly_prices(today, tomorrow, False, forecast)[-1]['estimated'])
        self.assertIsNone(m.parse_raw_points('bad'))
        self.assertEqual(m.parse_raw_points([None, {'hour': 'x', 'price': '1'}, {'hour': 'x', 'price': 1}]),
                         [{'hour': 'x', 'price': 1}])


class AdviceTests(unittest.TestCase):
    def test_defaults_running_and_forecast_exclusion(self):
        profiles = m.parse_run_profiles(fixture('run-profiles.json')['attributes'])
        timeline = hourly(home(), [2] * 4) + hourly(home() + 4 * HOUR, [.01] * 24, True)
        result = m.build_advice('washer', profiles, timeline, home(), 'Off')
        self.assertEqual(result['attributes']['programme'], 'bomuld|60')
        self.assertEqual(result['attributes']['kind'], 'now')
        self.assertFalse(result['attributes']['estimated'])
        self.assertEqual(result['advice'], 'Start now; waiting saves nothing.')
        for state in ('Running', 'Paused'):
            result = m.build_advice('washer', profiles, timeline, home(), state)
            self.assertEqual(result['state'], 'unknown')
            self.assertEqual(result['advice'], 'Already running.')
            self.assertNotIn('kind', result['attributes'])
        for profile, prices in ((profiles, []), (m.parse_run_profiles(None), timeline)):
            result = m.build_advice('washer', profile, prices, home(), 'Off')
            self.assertEqual(result['state'], 'unknown')
            self.assertNotIn('waiting saves nothing', result['advice'])

    def test_spoken_actions_keep_knob_window_and_home_clock(self):
        base = dict(kind='knob', knobMin=180, pressFromMs=home(), pressUntilMs=home(minute=30),
                    showPressBy=False, startMs=home(hour=23), costKr=.9, estimated=False)
        result = dict(plan=base, savingKr=2.1, noDelay={'kr': 3.0})
        sentence = m.advice_sentence(result)
        self.assertIn('Press now', sentence)
        self.assertIn('3 hours', sentence)
        self.assertIn('2.10 kr', sentence)
        base['showPressBy'] = True
        self.assertIn('press by 20:30', m.advice_sentence(result).lower())
        base.update(kind='wait', pressFromMs=home(minute=45), pressUntilMs=home(minute=48), knobMin=90)
        sentence = m.advice_sentence(result)
        self.assertIn('Wait until 20:45', sentence)
        self.assertIn('1.5 hours', sentence)
        self.assertIn('press by 20:48', sentence.lower())
        self.assertLessEqual(len(sentence), 200)
        # Copenhagen winter/summer offsets and both repeated autumn-hour instants.
        for iso, clock in [('2026-01-01T20:00:00+00:00', '21:00'),
                           ('2026-07-01T20:00:00+00:00', '22:00'),
                           ('2026-10-25T00:30:00+00:00', '02:30'),
                           ('2026-10-25T01:30:00+00:00', '02:30')]:
            base.update(pressFromMs=int(datetime.fromisoformat(iso).timestamp() * 1000), showPressBy=False)
            self.assertIn('Wait until ' + clock, m.advice_sentence(result))


class PublishingTests(unittest.TestCase):
    def setUp(self):
        name = 'appdaemon.plugins.hass.hassapi'
        if name not in sys.modules:
            for module in ('appdaemon', 'appdaemon.plugins', 'appdaemon.plugins.hass', name):
                sys.modules[module] = types.ModuleType(module)
            sys.modules[name].Hass = object
        self.module = importlib.import_module('start_advice')
        self.app = self.module.StartAdvice.__new__(self.module.StartAdvice)
        self.entities = {
            'sensor.appliance_run_profiles': {'state': 'published', 'attributes':
                {'schema': 1, 'appliances': {'washer': {'programmes': [
                    {'key': 'first', 'label': 'First', 'n': 5, 'p': ','.join(['100'] * 60)},
                    {'key': 'second', 'label': 'Second', 'n': 9, 'p': '100'}]},
                    'dishwasher': {'programmes': []}}}},
            'sensor.energi_data_service': {'state': '1', 'attributes': {
                'raw_today': [{'hour': '2026-10-09T18:00:00Z', 'price': 0},
                              {'hour': '2026-10-09T19:00:00Z', 'price': 0}]}},
            'sensor.washer_state': {'state': 'Off', 'attributes': {}},
            'sensor.dishwasher_state': {'state': 'Paused', 'attributes': {}},
        }
        self.writes, self.listeners, self.timers = [], [], []

        def get_state(entity, attribute=None):
            current = self.entities.get(entity)
            if attribute == 'all':
                return current
            return (current or {}).get('attributes' if attribute == 'attributes' else 'state')

        def set_state(entity, **payload):
            self.assertTrue(payload.pop('replace'))
            self.assertFalse(payload.pop('check_existence'))
            self.entities[entity] = payload
            self.writes.append((entity, payload))

        self.app.args = {}
        self.app.get_state = get_state
        self.app.set_state = set_state
        self.app.listen_state = lambda *a, **kw: self.listeners.append((a, kw))
        self.app.run_every = lambda *a: self.timers.append(a)
        self.app.log = lambda *a, **kw: None

    def test_schedule_transport_dedup_running_and_attribute_removal(self):
        with patch.object(self.module.time, 'time', return_value=home() / 1000):
            self.app.initialize()
            self.assertEqual(len(self.writes), 4)
            self.assertEqual(self.timers[0][1:], ('now+60', 60))
            self.assertEqual({a[1] for a, kw in self.listeners}, {
                'sensor.appliance_run_profiles', 'sensor.energi_data_service',
                'sensor.washer_state', 'sensor.dishwasher_state'})
            self.assertTrue(all(kw == {'attribute': 'all'} for a, kw in self.listeners))
            best = self.entities['sensor.washer_best_start']
            self.assertEqual(best['state'], '2026-10-09T18:00:00.000Z')
            self.assertEqual(best['attributes']['programme'], 'first')
            self.assertEqual(best['attributes']['knob_min'], '0')
            self.assertEqual(best['attributes']['cost_kr'], '0')
            self.assertEqual(best['attributes']['baseline_kr'], '0')
            self.assertEqual(best['attributes']['estimated'], 'false')
            self.assertNotIn('saving_kr', best['attributes'])
            self.assertEqual(self.entities['sensor.dishwasher_start_advice']['state'], 'Already running.')
            self.app._recompute()
            self.assertEqual(len(self.writes), 4)
            del self.entities['sensor.washer_start_advice']
            self.app._recompute()
            self.assertEqual(len(self.writes), 5)
            self.entities['sensor.washer_state']['state'] = 'Running'
            self.app._changed('sensor.washer_state', 'all', None, None, {})
            self.assertEqual(self.entities['sensor.washer_best_start'], {
                'state': 'unknown', 'attributes': {'device_class': 'timestamp'}})
            self.assertEqual(self.entities['sensor.washer_start_advice']['state'], 'Already running.')

    def test_failed_publish_retries(self):
        self.app._published = {}
        with patch.object(self.app, 'set_state', side_effect=RuntimeError('offline')):
            self.app._publish('sensor.washer_start_advice', 'Already running.', {})
        self.assertEqual(self.app._published, {})
        self.app._publish('sensor.washer_start_advice', 'Already running.', {})
        self.assertEqual(len(self.writes), 1)


if __name__ == '__main__':
    unittest.main()
