"""Project configuration checks that protect the mission sources and the emulated hardware."""
import copy
import json

import pytest

from project import model

from conftest import ROOT

DEVICE = {'name': 'Thermometer', 'id': 'tmp105', 'model': 'tmp105', 'bus': 'i2c-bus.1', 'address': 0x48,
          'values': {'temperature': 23500}}
CONFIG = {'schema_version': 1, 'board': 'raspi4b', 'os': 'raspios', 'devices': [DEVICE], 'apps': []}


def config(**changes):
    return dict(copy.deepcopy(CONFIG), **changes)


def device(**changes):
    return dict(copy.deepcopy(DEVICE), **changes)


@pytest.mark.parametrize('relative', ['/etc/passwd', '../outside', 'cFS/../../outside'])
def test_safe_path_rejects_paths_outside_the_project(tmp_path, relative):
    with pytest.raises(model.ProjectError, match='Unsafe path'):
        model.safe_path(tmp_path, relative)


def test_safe_path_rejects_symlinks(tmp_path):
    (tmp_path / 'real').mkdir()
    (tmp_path / 'link').symlink_to(tmp_path / 'real')
    with pytest.raises(model.ProjectError, match='Refusing symlink'):
        model.safe_path(tmp_path, 'link/file.c')
    assert model.safe_path(tmp_path, 'real/file.c') == tmp_path / 'real/file.c'


@pytest.mark.parametrize('name', ['sample_app', 'a', 'app2', 'a' * 19])
def test_identifier_accepts_snake_case(name):
    assert model.identifier(name) == name


@pytest.mark.parametrize('name', ['', 'Sample', '2app', 'app__x', 'app_', 'app-x', 'a' * 20, None, 5])
def test_identifier_rejects_other_names(name):
    with pytest.raises(model.ProjectError):
        model.identifier(name)


def test_checked_in_project_is_valid():
    assert model.load_config(ROOT)['schema_version'] == 1


def test_project_without_config_file_is_native(tmp_path):
    assert model.load_config(tmp_path) == {'schema_version': 1, 'board': None, 'os': None, 'devices': [], 'apps': []}


def test_valid_board_configuration_passes():
    model.validate_hardware(config(), ROOT)


@pytest.mark.parametrize('changes, message', [
    ({'schema_version': 2}, 'Unsupported project schema_version'),
    ({'schema_version': True}, 'Unsupported project schema_version'),
    ({'os': 'windows'}, 'Unsupported OS'),
    ({'board': 'unknown_board'}, 'No platform capability catalog'),
    ({'devices': {}}, 'devices and apps must be lists'),
    ({'devices': [device(model='bme280')]}, 'Unsupported device model'),
    ({'devices': [device(bus='i2c-bus.9')]}, 'Unsupported bus'),
    ({'devices': [device(address=0x07)]}, 'I2C address'),
    ({'devices': [device(address=0x78)]}, 'I2C address'),
    ({'devices': [device(address='0x48')]}, 'I2C address'),
    ({'devices': [device(name=' ')]}, 'Device name'),
    ({'devices': [device(id='Bad-Id')]}, 'Identifiers must use snake_case'),
    ({'devices': [device(), device(id='second')]}, 'Duplicate device ID or I2C bus/address'),
    ({'devices': [device(), device(address=0x49)]}, 'Duplicate device ID or I2C bus/address'),
    ({'devices': [device(values={'temperature': 127938})]}, 'Unsupported value'),
    ({'devices': [device(values={'temperature': 23.5})]}, 'Unsupported value'),
    ({'devices': [device(values={'humidity': 1})]}, 'Unsupported value'),
])
def test_invalid_board_configuration_is_rejected(changes, message):
    with pytest.raises(model.ProjectError, match=message):
        model.validate_hardware(config(**changes), ROOT)


def test_missing_or_extra_fields_are_rejected():
    incomplete = config()
    del incomplete['apps']
    with pytest.raises(model.ProjectError, match='project config fields must be'):
        model.validate_hardware(incomplete, ROOT)
    with pytest.raises(model.ProjectError, match='device fields must be'):
        model.validate_hardware(config(devices=[device(extra=1)]), ROOT)


def test_native_project_cannot_have_hardware():
    model.validate_hardware(config(board=None, os=None, devices=[]), ROOT)
    with pytest.raises(model.ProjectError, match='Select a board and operating system'):
        model.validate_hardware(config(board=None, os=None), ROOT)


def test_sensor_limits_come_from_the_board_catalog():
    limits = model.board_catalog('raspi4b', ROOT)['models']['tmp105']['properties']['temperature']
    model.validate_values(DEVICE, {'temperature': limits['max']}, model.board_catalog('raspi4b', ROOT))
    with pytest.raises(model.ProjectError):
        model.validate_values(DEVICE, {'temperature': limits['min'] - 1}, model.board_catalog('raspi4b', ROOT))


def test_board_catalog_filename_must_match_its_identifier(tmp_path):
    boards = tmp_path / 'project/boards'
    boards.mkdir(parents=True)
    (boards / 'other.json').write_text(json.dumps({'board': 'raspi4b'}))
    with pytest.raises(model.ProjectError, match='differs from its filename'):
        model.board_catalog('other', tmp_path)
