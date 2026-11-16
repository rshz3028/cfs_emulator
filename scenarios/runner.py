"""Run scripted command and telemetry scenarios against a running cFS and report pass or fail."""
import contextlib
import io
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

DEFAULT_TIMEOUT = 10
MAX_TIMEOUT = 600
EVENT_KEYS = {'app', 'contains', 'matches', 'event_id'}
COMPARISONS = {'equals', 'at_least', 'at_most', 'increases_by'}
TELEMETRY_KEYS = {'packet', 'field'} | COMPARISONS


class ScenarioError(ValueError):
    """The scenario file is invalid or does not fit the connected build."""


def _number(value):
    return type(value) in (int, float)


def load(path):
    """Read one scenario file and check its structure; the catalog is checked by check()."""
    try:
        scenario = json.loads(Path(path).read_text())
    except OSError as error:
        raise ScenarioError(f'{path}: {error.strerror or error}') from error
    except ValueError as error:
        raise ScenarioError(f'{path}: not valid JSON: {error}') from error
    errors = _structure(scenario)
    if errors:
        raise ScenarioError(f'{path}: ' + '; '.join(errors))
    return scenario


def _structure(scenario):
    if not isinstance(scenario, dict):
        return ['a scenario is a JSON object']
    errors = []
    unknown = set(scenario) - {'name', 'description', 'requirements', 'steps'}
    if unknown:
        errors.append('unknown keys: ' + ', '.join(sorted(unknown)))
    if not isinstance(scenario.get('name'), str) or not scenario['name'].strip():
        errors.append('"name" must be a nonempty string')
    if not isinstance(scenario.get('description', ''), str):
        errors.append('"description" must be a string')
    requirements = scenario.get('requirements', [])
    if not isinstance(requirements, list) or not all(isinstance(item, str) and item for item in requirements):
        errors.append('"requirements" must be a list of requirement identifiers')
    steps = scenario.get('steps')
    if not isinstance(steps, list) or not steps:
        return errors + ['"steps" must be a nonempty list']
    for index, step in enumerate(steps, 1):
        errors += [f'step {index}: {error}' for error in _step_structure(step)]
    return errors


def _step_structure(step):
    if not isinstance(step, dict):
        return ['a step is a JSON object']
    actions = [key for key in ('send', 'expect_event', 'expect_telemetry', 'wait') if key in step]
    if len(actions) != 1:
        return ['a step has exactly one of "send", "expect_event", "expect_telemetry" or "wait"']
    action = actions[0]
    allowed = {'send': {'send', 'values'}, 'wait': {'wait'}}.get(action, {action, 'timeout'})
    errors = []
    if set(step) - allowed:
        errors.append('unknown keys: ' + ', '.join(sorted(set(step) - allowed)))
    if 'timeout' in step and not (_number(step['timeout']) and 0 < step['timeout'] <= MAX_TIMEOUT):
        errors.append(f'"timeout" must be a number of seconds up to {MAX_TIMEOUT}')
    body = step[action]
    if action == 'send':
        if not isinstance(body, str) or not body:
            errors.append('"send" names one command of the catalog')
        if not isinstance(step.get('values', {}), dict):
            errors.append('"values" must be an object of command field values')
    elif action == 'wait':
        if not (_number(body) and 0 < body <= MAX_TIMEOUT):
            errors.append(f'"wait" must be a number of seconds up to {MAX_TIMEOUT}')
    elif action == 'expect_event':
        if not isinstance(body, dict) or not body or set(body) - EVENT_KEYS:
            errors.append('"expect_event" takes "app", "contains", "matches" and "event_id"')
        else:
            for key in ('app', 'contains', 'matches'):
                if key in body and (not isinstance(body[key], str) or not body[key]):
                    errors.append(f'"{key}" must be a nonempty string')
            if 'event_id' in body and type(body['event_id']) is not int:
                errors.append('"event_id" must be an integer')
            if isinstance(body.get('matches'), str):
                try:
                    re.compile(body['matches'])
                except re.error as error:
                    errors.append(f'"matches" is not a valid regular expression: {error}')
    else:
        if not isinstance(body, dict) or set(body) - TELEMETRY_KEYS or not isinstance(body.get('packet'), str):
            errors.append('"expect_telemetry" takes "packet", "field" and one comparison: '
                          + ', '.join(sorted(COMPARISONS)))
        else:
            used = COMPARISONS & set(body)
            if used and not isinstance(body.get('field'), str):
                errors.append('a comparison needs "field"')
            if 'field' in body and not used:
                errors.append('"field" needs a comparison: ' + ', '.join(sorted(COMPARISONS)))
            if len(used) > 1 and used != {'at_least', 'at_most'}:
                errors.append('use one comparison, or "at_least" together with "at_most"')
            for key in used - {'equals'}:
                if not _number(body[key]):
                    errors.append(f'"{key}" must be a number')
    return errors


def check(scenario, catalog):
    """Errors that would make the scenario meaningless on this build; nothing is sent if any exist."""
    commands = {command['id']: command for command in catalog['commands']}
    packets = {packet['name']: packet for packet in catalog['packets']}
    errors = []
    for index, step in enumerate(scenario['steps'], 1):
        if 'send' in step:
            command = commands.get(step['send'])
            if command is None:
                errors.append(f'step {index}: command {step["send"]} is not in this build')
                continue
            fields = {field['name'] for field in command['fields']}
            for name in sorted(set(step.get('values', {})) - fields):
                errors.append(f'step {index}: {step["send"]} has no field {name}')
        elif 'expect_telemetry' in step:
            body = step['expect_telemetry']
            packet = packets.get(body['packet'])
            if packet is None:
                errors.append(f'step {index}: packet {body["packet"]} is not in this build')
            elif 'field' in body and body['field'] not in {field['name'] for field in packet['fields']}:
                errors.append(f'step {index}: {body["packet"]} has no field {body["field"]}')
    return errors


class Session:
    """Telemetry seen by one connection, split at the last group of sent commands.

    Expectations only accept telemetry received after that group, so an event or a
    counter left over from an earlier step cannot satisfy a later one.
    """

    def __init__(self, ground, initial=()):
        self.ground = ground
        self.before = {}
        self.after = []
        self.cursor = 0
        self.last_was_send = False
        for packet in initial:
            self.before[packet['name']] = packet
            self.cursor = max(self.cursor, packet['event_id'])

    def drain(self, timeout=0):
        packets = self.ground.packets_after(self.cursor, timeout)
        if packets:
            self.cursor = packets[-1]['event_id']
            self.after += packets
        return packets

    def mark(self):
        self.drain()
        for packet in self.after:
            self.before[packet['name']] = packet
        self.after = []

    def wait_for(self, match, timeout):
        """First packet after the mark that match() accepts, or None at the deadline."""
        deadline = time.monotonic() + timeout
        checked = 0
        while True:
            for packet in self.after[checked:]:
                if match(packet):
                    return packet
            checked = len(self.after)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if not self.drain(min(remaining, 1)) and not self.ground.connected():
                raise ConnectionError('The telemetry link closed.')

    def step(self, step):
        """Run one step and return (passed, detail)."""
        sending = 'send' in step
        if sending and not self.last_was_send:
            self.mark()
        self.last_was_send = sending
        if sending:
            sent = self.ground.send(step['send'], step.get('values', {}))
            return True, f'sent {sent["bytes"]} bytes to MID 0x{sent["mid"]:04X}, command code {sent["code"]}'
        if 'wait' in step:
            deadline = time.monotonic() + step['wait']
            while time.monotonic() < deadline:
                if not self.drain(min(deadline - time.monotonic(), 1)) and not self.ground.connected():
                    raise ConnectionError('The telemetry link closed.')
            return True, f'waited {step["wait"]} s'
        timeout = step.get('timeout', DEFAULT_TIMEOUT)
        if 'expect_event' in step:
            return self._event(step['expect_event'], timeout)
        return self._telemetry(step['expect_telemetry'], timeout)

    def _event(self, expected, timeout):
        pattern = re.compile(expected['matches']) if 'matches' in expected else None

        def match(packet):
            values = packet['values']
            if 'PacketID.AppName' not in values:
                return False
            message = str(values.get('Message', ''))
            return (('app' not in expected or str(values['PacketID.AppName']).lower() == expected['app'].lower())
                    and ('event_id' not in expected or values.get('PacketID.EventID') == expected['event_id'])
                    and ('contains' not in expected or expected['contains'] in message)
                    and (pattern is None or pattern.search(message)))

        packet = self.wait_for(match, timeout)
        if packet:
            values = packet['values']
            return True, f'{values["PacketID.AppName"]} event {values.get("PacketID.EventID")}: {values.get("Message", "")}'
        events = [packet['values'] for packet in self.after if 'PacketID.AppName' in packet['values']]
        seen = '; '.join(f'{values["PacketID.AppName"]} {values.get("PacketID.EventID")}: {values.get("Message", "")}'
                         for values in events[-5:])
        return False, (f'no matching event in {timeout} s; ' +
                       (f'last events received: {seen}' if seen else 'no events were received'))

    def _telemetry(self, expected, timeout):
        name, field = expected['packet'], expected.get('field')
        baseline = None
        if 'increases_by' in expected:
            previous = self.before.get(name)
            if previous is None or not _number(previous['values'].get(field)):
                return False, (f'no {name}.{field} value was received before the command, so there is nothing to '
                               f'compare with; add an expect_telemetry step for {name} before the send')
            baseline = previous['values'][field]

        def match(packet):
            if packet['name'] != name:
                return False
            if field is None:
                return True
            value = packet['values'].get(field)
            if 'equals' in expected:
                return value == expected['equals']
            if not _number(value):
                return False
            if baseline is not None:
                return value == baseline + expected['increases_by']
            return expected.get('at_least', value) <= value <= expected.get('at_most', value)

        packet = self.wait_for(match, timeout)
        if packet:
            return True, f'{name} received' + (f', {field} = {packet["values"][field]!r}' if field else '')
        received = [packet for packet in self.after if packet['name'] == name]
        if not received:
            return False, f'{name} was not received in {timeout} s'
        detail = f'{name}.{field} was {received[-1]["values"].get(field)!r} in the last of {len(received)} packets'
        if baseline is not None:
            detail += f'; expected {baseline + expected["increases_by"]!r} ({baseline!r} before the command)'
        return False, detail + f'; waited {timeout} s'


def run_scenario(session, scenario, emit=print):
    """Run the steps in order and stop at the first failure; later steps are reported as not run."""
    session.mark()
    session.last_was_send = False
    started = time.monotonic()
    steps, failed = [], False
    for index, step in enumerate(scenario['steps'], 1):
        action = next(key for key in ('send', 'expect_event', 'expect_telemetry', 'wait') if key in step)
        record = {'index': index, 'action': action, 'step': step, 'duration': 0.0}
        if failed:
            record.update(status='not run', detail='an earlier step failed')
        else:
            began = time.monotonic()
            try:
                passed, detail = session.step(step)
            except (ValueError, ConnectionError) as error:
                passed, detail = False, str(error)
            record.update(status='passed' if passed else 'failed', detail=detail,
                          duration=round(time.monotonic() - began, 3))
            failed = not passed
        steps.append(record)
        emit(f'  {record["status"].upper():8} step {index} {action} {json.dumps(step[action])}: {record["detail"]}')
    return {'name': scenario['name'], 'description': scenario.get('description', ''),
            'requirements': scenario.get('requirements', []), 'verdict': 'failed' if failed else 'passed',
            'duration': round(time.monotonic() - started, 3), 'steps': steps}


def junit(report):
    """JUnit XML with one test suite per scenario and one test case per step."""
    root = ElementTree.Element('testsuites', name='Flight Studio scenarios')
    for scenario in report['scenarios']:
        steps = scenario['steps']
        suite = ElementTree.SubElement(
            root, 'testsuite', name=scenario['name'], tests=str(len(steps)),
            failures=str(sum(step['status'] == 'failed' for step in steps)),
            skipped=str(sum(step['status'] == 'not run' for step in steps)),
            time=str(scenario['duration']), timestamp=report['started_at'])
        properties = ElementTree.SubElement(suite, 'properties')
        for key, value in (('file', scenario['file']), ('target', report['target']),
                           ('build_fingerprint', report['build_fingerprint']), ('cfs_commit', report['cfs_commit']),
                           ('requirements', ' '.join(scenario['requirements']))):
            ElementTree.SubElement(properties, 'property', name=key, value=str(value or ''))
        for step in steps:
            case = ElementTree.SubElement(suite, 'testcase', classname=scenario['name'], time=str(step['duration']),
                                          name=f'{step["index"]} {step["action"]} {json.dumps(step["step"][step["action"]])}')
            if step['status'] == 'failed':
                ElementTree.SubElement(case, 'failure', message=step['detail'])
            elif step['status'] == 'not run':
                ElementTree.SubElement(case, 'skipped', message=step['detail'])
    return ElementTree.tostring(root, encoding='unicode', xml_declaration=True)


def _commit(root):
    try:
        result = subprocess.run(['git', '-C', str(Path(root) / 'cFS'), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def run_files(root, files, target, connection=None, emit=print):
    """Connect the Ground Station to the running cFS and run each scenario file once.

    Raises ScenarioError before sending anything when a file does not fit the build.
    """
    scenarios = [(str(path), load(path)) for path in files]
    # The NASA GroundSystem modules print every packet they send; keep the report readable.
    with contextlib.redirect_stdout(io.StringIO()):
        from interface.server import Studio
        studio = Studio(root)
        try:
            options = {'target': target}
            if connection is not None:
                options['connection'] = connection
            snapshot = studio.ground_start(options)
            catalog = studio.ground_catalog(target)
            errors = [f'{path}: {error}' for path, scenario in scenarios for error in check(scenario, catalog)]
            if errors:
                raise ScenarioError('\n'.join(errors))
            report = {'tool': 'Flight Studio scenario runner', 'target': target,
                      'build_fingerprint': catalog.get('build_fingerprint'), 'cfs_commit': _commit(root),
                      'started_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                      'subscription_errors': snapshot['subscription_errors'], 'scenarios': []}
            session = Session(studio.ground, snapshot['packets'])
            for path, scenario in scenarios:
                emit(f'Scenario: {scenario["name"]} ({path})')
                result = dict(run_scenario(session, scenario, emit), file=path)
                emit(f'  {result["verdict"].upper()} in {result["duration"]} s')
                report['scenarios'].append(result)
        finally:
            studio.close()
    report['finished_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    report['verdict'] = 'passed' if all(item['verdict'] == 'passed' for item in report['scenarios']) else 'failed'
    return report
