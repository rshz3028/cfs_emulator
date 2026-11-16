"""Terminal output paging and the command line."""
import base64
import random
import subprocess
import sys
import threading
from collections import deque

import pytest

from interface.backend.terminal import Shell

from conftest import ROOT


def shell(chunks, dropped=0):
    """A Shell holding output without a child process; `dropped` bytes already left the backlog."""
    instance = Shell.__new__(Shell)
    instance.lock = threading.Lock()
    instance.chunks = deque(chunks)
    instance.start = dropped
    instance.end = dropped + sum(len(chunk) for chunk in chunks)
    instance.exit_code = None
    return instance


def test_read_returns_the_same_bytes_as_joining_the_backlog():
    generator = random.Random(7)
    for _ in range(200):
        chunks = [bytes(generator.randrange(256) for _ in range(generator.randrange(1, 40)))
                  for _ in range(generator.randrange(0, 30))]
        dropped = generator.randrange(0, 50)
        instance = shell(chunks, dropped)
        cursor = generator.randrange(0, instance.end + 1)
        result = instance.read(cursor)
        offset = max(cursor, dropped)
        expected = b''.join(chunks)[offset - dropped:offset - dropped + 65536]
        assert base64.b64decode(result['data']) == expected
        assert result['cursor'] == offset + len(expected)
        assert result['truncated'] == (cursor < dropped)


def test_read_returns_at_most_one_window_and_continues_from_its_cursor():
    chunks = [bytes([index]) * 30000 for index in range(5)]
    instance = shell(chunks)
    first = instance.read(0)
    assert len(base64.b64decode(first['data'])) == 65536
    data = base64.b64decode(first['data'])
    cursor = first['cursor']
    while cursor < instance.end:
        part = instance.read(cursor)
        data += base64.b64decode(part['data'])
        cursor = part['cursor']
    assert data == b''.join(chunks)


@pytest.mark.parametrize('cursor', [-1, 1.5, '0', None, 11])
def test_read_rejects_invalid_cursors(cursor):
    with pytest.raises(ValueError):
        shell([b'0123456789']).read(cursor)


def start(*arguments):
    return subprocess.run([sys.executable, str(ROOT / 'start'), *arguments], capture_output=True, text=True, timeout=60)


def test_help_lists_both_ways_to_start():
    result = start('--help')
    assert result.returncode == 0
    assert './start [--ui] [--port PORT]' in result.stdout and './start --no-ui COMMAND' in result.stdout


def test_command_line_help_lists_the_scenario_command():
    result = start('--no-ui', '--help')
    assert result.returncode == 0 and 'scenario' in result.stdout


@pytest.mark.parametrize('arguments', [
    ('--no-ui', 'scenario'),                                    # no scenario file
    ('--no-ui', 'profile', '--board', 'raspi4b'),               # board without OS
    ('--no-ui', 'build', '--target', 'native', '--host', 'x'),  # SSH option without a board
    ('--no-ui', 'values', '--target', 'native'),                # sensor values exist only in QEMU
    ('--no-ui', 'unknown-command'),
])
def test_invalid_command_lines_exit_with_usage_error(arguments):
    assert start(*arguments).returncode == 2


def test_scenario_file_is_checked_before_connecting(tmp_path):
    result = start('--no-ui', 'scenario', str(tmp_path / 'missing.json'), '--target', 'native')
    assert result.returncode == 1
    assert 'missing.json' in result.stderr and result.stdout == ''
