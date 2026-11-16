"""Scenario files: structure checks, catalog checks, step verdicts and the reports."""
import json
from xml.etree import ElementTree

import pytest

from scenarios import runner

HK = 'SAMPLE_APP_HkTlm_t'
EVENTS = 'CFE_EVS_LongEventTlm_t'
CATALOG = {
    'commands': [{'id': 'SAMPLE_APP_NOOP_CC', 'fields': []},
                 {'id': 'TO_LAB_OUTPUT_ENABLE_CC', 'fields': [{'name': 'dest_IP'}]}],
    'packets': [{'name': HK, 'fields': [{'name': 'CommandCounter'}, {'name': 'CommandErrorCounter'}]}],
}


class FakeGround:
    """Stands in for GroundStation: telemetry is pushed by the test or by a reaction to a command."""

    def __init__(self):
        self.history, self.sent, self.reactions = [], [], {}
        self.sequence = 0
        self.up = True

    def push(self, name, **values):
        self.sequence += 1
        self.history.append({'name': name, 'values': values, 'event_id': self.sequence})

    def event(self, app, message, event_id=1):
        self.push(EVENTS, **{'PacketID.AppName': app, 'PacketID.EventID': event_id, 'Message': message})

    def packets_after(self, event_id, timeout=0):
        return [packet for packet in self.history if packet['event_id'] > event_id]

    def connected(self):
        return self.up

    def send(self, command_id, values):
        if not self.up:
            raise ValueError('Connect the ground station before sending commands')
        self.sent.append((command_id, values))
        self.sequence += 1
        if command_id in self.reactions:
            self.reactions[command_id](self)
        return {'bytes': 8, 'mid': 0x1882, 'code': 0}


def scenario(*steps):
    return {'name': 'test', 'steps': list(steps)}


def run(ground, *steps):
    result = runner.run_scenario(runner.Session(ground), scenario(*steps), emit=lambda line: None)
    return result, [step['status'] for step in result['steps']]


def noop_accepted(ground):
    ground.event('SAMPLE_APP', 'SAMPLE: NOOP command v1', event_id=3)
    ground.push(HK, CommandCounter=5, CommandErrorCounter=0)


@pytest.fixture
def ground():
    ground = FakeGround()
    ground.push(HK, CommandCounter=4, CommandErrorCounter=0)
    return ground


# Structure of a scenario file

@pytest.mark.parametrize('content, message', [
    ([], 'a scenario is a JSON object'),
    ({'steps': [{'wait': 1}]}, '"name" must be a nonempty string'),
    ({'name': 'x', 'steps': []}, '"steps" must be a nonempty list'),
    ({'name': 'x', 'steps': [{'wait': 1}], 'extra': 1}, 'unknown keys: extra'),
    ({'name': 'x', 'requirements': 'FS-1', 'steps': [{'wait': 1}]}, '"requirements" must be a list'),
    ({'name': 'x', 'steps': [{'sned': 'A'}]}, 'step 1: a step has exactly one of'),
    ({'name': 'x', 'steps': [{'send': 'A', 'wait': 1}]}, 'step 1: a step has exactly one of'),
    ({'name': 'x', 'steps': [{'send': 'A', 'timeout': 5}]}, 'step 1: unknown keys: timeout'),
    ({'name': 'x', 'steps': [{'send': 'A', 'values': [1]}]}, '"values" must be an object'),
    ({'name': 'x', 'steps': [{'wait': 0}]}, '"wait" must be a number of seconds'),
    ({'name': 'x', 'steps': [{'wait': True}]}, '"wait" must be a number of seconds'),
    ({'name': 'x', 'steps': [{'expect_event': {'app': 'A'}, 'timeout': 601}]}, '"timeout" must be a number'),
    ({'name': 'x', 'steps': [{'expect_event': {}}]}, '"expect_event" takes'),
    ({'name': 'x', 'steps': [{'expect_event': {'matches': '('}}]}, 'not a valid regular expression'),
    ({'name': 'x', 'steps': [{'expect_telemetry': {'field': 'A', 'equals': 1}}]}, '"expect_telemetry" takes'),
    ({'name': 'x', 'steps': [{'expect_telemetry': {'packet': 'P', 'field': 'A'}}]}, '"field" needs a comparison'),
    ({'name': 'x', 'steps': [{'expect_telemetry': {'packet': 'P', 'equals': 1}}]}, 'a comparison needs "field"'),
    ({'name': 'x', 'steps': [{'expect_telemetry': {'packet': 'P', 'field': 'A', 'equals': 1, 'at_least': 1}}]},
     'use one comparison'),
    ({'name': 'x', 'steps': [{'expect_telemetry': {'packet': 'P', 'field': 'A', 'increases_by': '1'}}]},
     '"increases_by" must be a number'),
])
def test_load_rejects_invalid_structure(tmp_path, content, message):
    path = tmp_path / 'scenario.json'
    path.write_text(json.dumps(content))
    with pytest.raises(runner.ScenarioError, match=message):
        runner.load(path)


def test_load_reports_missing_file_and_invalid_json(tmp_path):
    with pytest.raises(runner.ScenarioError, match='missing.json'):
        runner.load(tmp_path / 'missing.json')
    (tmp_path / 'broken.json').write_text('{')
    with pytest.raises(runner.ScenarioError, match='not valid JSON'):
        runner.load(tmp_path / 'broken.json')


def test_repository_scenarios_are_valid():
    files = sorted((runner.Path(__file__).resolve().parents[1] / 'scenarios').glob('*.json'))
    assert files
    for path in files:
        assert runner.load(path)['steps'], path


# Fit with the connected build

def test_check_accepts_names_of_the_catalog():
    assert runner.check(scenario(
        {'send': 'TO_LAB_OUTPUT_ENABLE_CC', 'values': {'dest_IP': '127.0.0.1'}},
        {'expect_telemetry': {'packet': HK, 'field': 'CommandCounter', 'equals': 1}},
        {'expect_event': {'app': 'ANY_APP'}}, {'wait': 1}), CATALOG) == []


def test_check_reports_every_name_missing_from_the_build():
    assert runner.check(scenario(
        {'send': 'MISSING_CC'},
        {'send': 'SAMPLE_APP_NOOP_CC', 'values': {'Extra': 1}},
        {'expect_telemetry': {'packet': 'Missing_t'}},
        {'expect_telemetry': {'packet': HK, 'field': 'Missing', 'equals': 1}}), CATALOG) == [
            'step 1: command MISSING_CC is not in this build',
            'step 2: SAMPLE_APP_NOOP_CC has no field Extra',
            'step 3: packet Missing_t is not in this build',
            f'step 4: {HK} has no field Missing']


# Step verdicts

def test_command_confirmed_by_event_and_counter_passes(ground):
    ground.reactions['SAMPLE_APP_NOOP_CC'] = noop_accepted
    result, statuses = run(
        ground,
        {'send': 'SAMPLE_APP_NOOP_CC'},
        {'expect_event': {'app': 'sample_app', 'contains': 'NOOP command', 'event_id': 3}, 'timeout': .05},
        {'expect_event': {'matches': r'NOOP command v\d+$'}, 'timeout': .05},
        {'expect_telemetry': {'packet': HK, 'field': 'CommandCounter', 'increases_by': 1}, 'timeout': .05},
        {'expect_telemetry': {'packet': HK, 'field': 'CommandErrorCounter', 'equals': 0}, 'timeout': .05},
        {'expect_telemetry': {'packet': HK, 'field': 'CommandCounter', 'at_least': 5, 'at_most': 5}, 'timeout': .05})
    assert statuses == ['passed'] * 6
    assert result['verdict'] == 'passed'
    assert ground.sent == [('SAMPLE_APP_NOOP_CC', {})]


def test_counter_that_does_not_move_fails_and_reports_expected_value(ground):
    ground.reactions['SAMPLE_APP_NOOP_CC'] = lambda ground: ground.push(HK, CommandCounter=4, CommandErrorCounter=1)
    result, statuses = run(
        ground, {'send': 'SAMPLE_APP_NOOP_CC'},
        {'expect_telemetry': {'packet': HK, 'field': 'CommandCounter', 'increases_by': 1}, 'timeout': .05})
    assert statuses == ['passed', 'failed']
    assert result['verdict'] == 'failed'
    assert 'expected 5 (4 before the command)' in result['steps'][1]['detail']


def test_increase_needs_a_value_received_before_the_command():
    ground = FakeGround()
    ground.reactions['SAMPLE_APP_NOOP_CC'] = noop_accepted
    result, statuses = run(
        ground, {'send': 'SAMPLE_APP_NOOP_CC'},
        {'expect_telemetry': {'packet': HK, 'field': 'CommandCounter', 'increases_by': 1}, 'timeout': .05})
    assert statuses == ['passed', 'failed']
    assert 'add an expect_telemetry step' in result['steps'][1]['detail']


def test_event_received_before_the_command_does_not_satisfy_the_expectation(ground):
    ground.event('SAMPLE_APP', 'SAMPLE: NOOP command v1')
    result, statuses = run(
        ground, {'send': 'SAMPLE_APP_NOOP_CC'},
        {'expect_event': {'app': 'SAMPLE_APP', 'contains': 'NOOP command'}, 'timeout': .05})
    assert statuses == ['passed', 'failed']
    assert 'no events were received' in result['steps'][1]['detail']


def test_failed_event_expectation_lists_the_events_that_did_arrive(ground):
    ground.reactions['SAMPLE_APP_NOOP_CC'] = lambda ground: ground.event('SAMPLE_APP', 'Invalid command code', 7)
    result, _ = run(ground, {'send': 'SAMPLE_APP_NOOP_CC'},
                    {'expect_event': {'app': 'SAMPLE_APP', 'contains': 'NOOP command'}, 'timeout': .05})
    assert 'SAMPLE_APP 7: Invalid command code' in result['steps'][1]['detail']


def test_consecutive_commands_share_one_starting_point(ground):
    ground.reactions['SAMPLE_APP_NOOP_CC'] = lambda ground: ground.event('SAMPLE_APP', 'first reply')
    ground.reactions['TO_LAB_OUTPUT_ENABLE_CC'] = lambda ground: ground.event('TO_LAB', 'second reply')
    _, statuses = run(
        ground, {'send': 'SAMPLE_APP_NOOP_CC'}, {'send': 'TO_LAB_OUTPUT_ENABLE_CC', 'values': {'dest_IP': '127.0.0.1'}},
        {'expect_event': {'contains': 'first reply'}, 'timeout': .05},
        {'expect_event': {'contains': 'second reply'}, 'timeout': .05})
    assert statuses == ['passed'] * 4


def test_reply_to_an_earlier_command_group_is_not_accepted(ground):
    ground.reactions['SAMPLE_APP_NOOP_CC'] = lambda ground: ground.event('SAMPLE_APP', 'reply')
    ground.reactions['TO_LAB_OUTPUT_ENABLE_CC'] = lambda ground: None
    _, statuses = run(
        ground, {'send': 'SAMPLE_APP_NOOP_CC'}, {'expect_event': {'contains': 'reply'}, 'timeout': .05},
        {'send': 'TO_LAB_OUTPUT_ENABLE_CC'}, {'expect_event': {'contains': 'reply'}, 'timeout': .05})
    assert statuses == ['passed', 'passed', 'passed', 'failed']


def test_missing_packet_fails(ground):
    result, statuses = run(ground, {'expect_telemetry': {'packet': 'Other_t'}, 'timeout': .05})
    assert statuses == ['failed']
    assert result['steps'][0]['detail'] == 'Other_t was not received in 0.05 s'


def test_steps_after_a_failure_are_not_run(ground):
    result, statuses = run(ground, {'expect_telemetry': {'packet': 'Other_t'}, 'timeout': .05},
                           {'send': 'SAMPLE_APP_NOOP_CC'}, {'wait': .01})
    assert statuses == ['failed', 'not run', 'not run']
    assert ground.sent == []


def test_closed_link_fails_the_step(ground):
    ground.up = False
    _, statuses = run(ground, {'wait': .05})
    assert statuses == ['failed']
    _, statuses = run(ground, {'send': 'SAMPLE_APP_NOOP_CC'})
    assert statuses == ['failed']
    _, statuses = run(ground, {'expect_event': {'app': 'A'}, 'timeout': 5})
    assert statuses == ['failed']


def test_wait_passes_while_connected(ground):
    _, statuses = run(ground, {'wait': .02})
    assert statuses == ['passed']


def test_telemetry_of_the_connection_is_the_baseline_of_the_first_scenario():
    ground = FakeGround()
    ground.sequence = 9
    ground.reactions['SAMPLE_APP_NOOP_CC'] = noop_accepted
    initial = [{'name': HK, 'values': {'CommandCounter': 4}, 'event_id': 9}]
    result = runner.run_scenario(runner.Session(ground, initial), scenario(
        {'send': 'SAMPLE_APP_NOOP_CC'},
        {'expect_telemetry': {'packet': HK, 'field': 'CommandCounter', 'increases_by': 1}, 'timeout': .05}),
        emit=lambda line: None)
    assert result['verdict'] == 'passed'


# Reports

def test_junit_counts_failures_and_steps_not_run(ground):
    result, _ = run(ground, {'wait': .01}, {'expect_telemetry': {'packet': 'Other_t'}, 'timeout': .05}, {'wait': .01})
    report = {'target': 'native', 'build_fingerprint': 'f' * 64, 'cfs_commit': None,
              'started_at': '2026-01-01T00:00:00+00:00',
              'scenarios': [dict(result, file='scenario.json', requirements=['REQ-1'])]}
    suite = ElementTree.fromstring(runner.junit(report)).find('testsuite')
    assert (suite.get('tests'), suite.get('failures'), suite.get('skipped')) == ('3', '1', '1')
    cases = suite.findall('testcase')
    assert cases[1].find('failure').get('message') == 'Other_t was not received in 0.05 s'
    assert cases[2].find('skipped') is not None
    properties = {item.get('name'): item.get('value') for item in suite.find('properties')}
    assert properties['requirements'] == 'REQ-1' and properties['cfs_commit'] == ''
