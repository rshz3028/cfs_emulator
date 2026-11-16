"""Deployment records and the command that starts a release on a board."""
import json
import os
import subprocess

import pytest

from hardware.manager import HardwareManager

BOARD_A = {'host': 'board-a', 'user': 'pi', 'port': 22}
BOARD_B = {'host': 'board-b', 'user': 'pi', 'port': 22}


@pytest.fixture
def manager(tmp_path):
    return HardwareManager(tmp_path)


def deployed(manager):
    return json.loads((manager.runtime / 'deployed.json').read_text())


def test_stopping_the_deployed_board_marks_it_stopped(manager):
    manager._save('deployed.json', {'physical': {'connection': BOARD_A}})
    manager._save_active('physical', BOARD_A, 'running')
    manager._mark_stopped('physical', BOARD_A)
    assert 'stopped_at' in deployed(manager)['physical']
    assert manager._load('active.json')['phase'] == 'stopped'


def test_stopping_another_board_keeps_the_deployed_board_running(manager):
    manager._save('deployed.json', {'physical': {'connection': BOARD_A}})
    manager._save_active('physical', BOARD_A, 'running')
    manager._mark_stopped('physical', BOARD_B)
    assert 'stopped_at' not in deployed(manager)['physical']
    assert manager._load('active.json')['phase'] == 'running'


def test_stopping_one_target_leaves_the_others(manager):
    manager._save('deployed.json', {'native': {}, 'qemu': {}})
    manager._mark_stopped('native')
    records = deployed(manager)
    assert 'stopped_at' in records['native'] and 'stopped_at' not in records['qemu']


def test_stopping_a_target_that_was_never_deployed_changes_nothing(manager):
    manager._mark_stopped('qemu')
    assert not (manager.runtime / 'deployed.json').exists()


def test_build_directories_keep_native_and_board_builds_apart():
    assert HardwareManager.build_directory('native') != HardwareManager.build_directory('qemu')
    assert HardwareManager.build_directory('qemu') == HardwareManager.build_directory('physical')


class Board:
    """A directory that stands in for the board, with stub pgrep, sudo and sleep on PATH."""

    def __init__(self, path, service_starts=True, running=False):
        self.directory = str(path / 'home pi' / 'cfs-arm64')    # the space checks the quoting
        self.releases = self.directory + '/releases'
        os.makedirs(self.directory + '/studio')
        bin_directory = path / 'bin'
        bin_directory.mkdir()
        for name, status in (('pgrep', 0 if running else 1), ('sudo', 0 if service_starts else 1), ('sleep', 0)):
            script = bin_directory / name
            script.write_text(f'#!/bin/sh\nexit {status}\n')
            script.chmod(0o755)
        self.environment = {'PATH': f'{bin_directory}:/usr/bin:/bin'}

    def upload(self, name):
        os.makedirs(f'{self.releases}/{name}')
        return f'{self.releases}/{name}'

    def activate(self, name):
        command = HardwareManager._activate_release_command(self.directory, self.upload(name))
        return subprocess.run(['sh', '-c', command], env=self.environment, capture_output=True, text=True)

    def kept(self):
        return sorted(os.listdir(self.releases))

    def current(self):
        return os.path.basename(os.readlink(self.directory + '/studio/current'))


def test_successful_start_keeps_the_new_release_and_the_one_it_replaced(tmp_path):
    board = Board(tmp_path)
    history = []
    for name in ('a', 'b', 'c', 'd'):
        assert board.activate(name).returncode == 0
        history.append(board.kept())
        assert board.current() == name
    assert history == [['a'], ['a', 'b'], ['b', 'c'], ['c', 'd']]


def test_successful_start_removes_failed_uploads(tmp_path):
    board = Board(tmp_path)
    board.activate('a')
    board.upload('interrupted-upload')
    assert board.activate('b').returncode == 0
    assert board.kept() == ['a', 'b']


def test_failed_start_reports_failure_and_removes_nothing(tmp_path):
    board = Board(tmp_path, service_starts=False)
    for name in ('a', 'b', 'c'):
        assert board.activate(name).returncode == 1
    assert board.kept() == ['a', 'b', 'c']


def test_start_is_refused_while_another_cfs_runs(tmp_path):
    board = Board(tmp_path, running=True)
    result = board.activate('a')
    assert result.returncode == 1 and 'Another cFS instance is running' in result.stdout
    assert not os.path.lexists(board.directory + '/studio/current')


def test_start_is_refused_when_current_is_not_a_link(tmp_path):
    board = Board(tmp_path)
    os.makedirs(board.directory + '/studio/current')
    assert board.activate('a').returncode != 0
    assert os.path.isdir(board.directory + '/studio/current') and not os.path.islink(board.directory + '/studio/current')
