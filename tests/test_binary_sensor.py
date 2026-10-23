from unittest.mock import Mock

import pytest
from homeassistant.helpers.entity_registry import DATA_REGISTRY, EntityRegistry
from homeassistant.loader import DATA_COMPONENTS, DATA_INTEGRATIONS

from custom_components.mypyllant import SystemCoordinator
from myPyllant.api import MyPyllantAPI
from myPyllant.models import System
from myPyllant.tests.generate_test_data import DATA_DIR
from myPyllant.tests.utils import list_test_data, load_test_data

from custom_components.mypyllant.binary_sensor import (
    AmbisenseDeviceLowBattery,
    AmbisenseDeviceUnreachable,
    CircuitIsCoolingAllowed,
    ControlError,
    ControlOnline,
    SystemControlEntity,
    async_setup_entry,
    ZoneIsManualCoolingActive,
)
from custom_components.mypyllant.utils import CircuitEntity
from custom_components.mypyllant.const import DOMAIN
from tests.utils import get_config_entry


@pytest.mark.parametrize("test_data", list_test_data(only_with_systems=True))
async def test_async_setup_binary_sensors(
    hass,
    mypyllant_aioresponses,
    mocked_api: MyPyllantAPI,
    system_coordinator_mock,
    test_data,
):
    hass.data[DATA_COMPONENTS] = {}
    hass.data[DATA_INTEGRATIONS] = {}
    hass.data[DATA_REGISTRY] = EntityRegistry(hass)
    with mypyllant_aioresponses(test_data) as _:
        config_entry = get_config_entry()
        system_coordinator_mock.data = (
            await system_coordinator_mock._async_update_data()
        )
        hass.data[DOMAIN] = {
            config_entry.entry_id: {"system_coordinator": system_coordinator_mock}
        }
        mock = Mock(return_value=None)
        await async_setup_entry(hass, config_entry, mock)
        mock.assert_called_once()
        assert len(mock.call_args.args[0]) > 0

        await mocked_api.aiohttp_session.close()


@pytest.mark.parametrize("test_data", list_test_data(only_with_systems=True))
async def test_system_binary_sensors(
    mypyllant_aioresponses, mocked_api: MyPyllantAPI, system_coordinator_mock, test_data
):
    with mypyllant_aioresponses(test_data) as _:
        system_coordinator_mock.data = (
            await system_coordinator_mock._async_update_data()
        )
        system = SystemControlEntity(0, system_coordinator_mock)
        assert isinstance(system.device_info, dict)

        circuit = CircuitEntity(0, 0, system_coordinator_mock)
        assert isinstance(circuit.device_info, dict)
        assert isinstance(circuit.system, System)

        assert isinstance(ControlError(0, system_coordinator_mock).is_on, bool)
        assert isinstance(ControlError(0, system_coordinator_mock).name, str)
        assert ControlOnline(0, system_coordinator_mock).is_on is True
        assert isinstance(ControlOnline(0, system_coordinator_mock).name, str)
        # TODO: May  moved to zones, see no_cooling.yaml
        # assert isinstance(
        #    CircuitIsCoolingAllowed(0, 0, system_coordinator_mock).is_on, bool
        # )
        assert isinstance(
            CircuitIsCoolingAllowed(0, 0, system_coordinator_mock).name, str
        )
        await mocked_api.aiohttp_session.close()


@pytest.mark.parametrize(
    "online_state,expected", [("ONLINE", True), ("OFFLINE", False), (None, None)]
)
async def test_control_online_without_connection_status(
    mypyllant_aioresponses,
    mocked_api: MyPyllantAPI,
    system_coordinator_mock: SystemCoordinator,
    online_state,
    expected,
):
    """
    The connection status is only fetched with an option, which is disabled by default.
    The online state of the home is used instead, see
    https://github.com/signalkraft/mypyllant-component/issues/500
    """
    test_data = load_test_data(DATA_DIR / "heatpump_heat_curve")
    with mypyllant_aioresponses(test_data) as _:
        system_coordinator_mock.data = (
            await system_coordinator_mock._async_update_data()
        )
        system = system_coordinator_mock.data[0]
        system.connected = None
        system.diagnostic_trouble_codes = None
        system.home.extra_fields.pop("online_state", None)
        if online_state is not None:
            system.home.extra_fields["online_state"] = online_state
        assert ControlOnline(0, system_coordinator_mock).is_on is expected
        assert ControlError(0, system_coordinator_mock).is_on is None
        await mocked_api.aiohttp_session.close()


async def test_control_error(
    mypyllant_aioresponses,
    mocked_api: MyPyllantAPI,
    system_coordinator_mock: SystemCoordinator,
):
    test_data = load_test_data(DATA_DIR / "ambisense2.yaml")
    with mypyllant_aioresponses(test_data) as _:
        system_coordinator_mock.data = (
            await system_coordinator_mock._async_update_data()
        )
        control_error = ControlError(0, system_coordinator_mock)
        assert control_error.is_on
        await mocked_api.aiohttp_session.close()


async def test_is_manual_cooling_active(
    mypyllant_aioresponses,
    mocked_api: MyPyllantAPI,
    system_coordinator_mock: SystemCoordinator,
):
    test_data = load_test_data(DATA_DIR / "ventilation")
    with mypyllant_aioresponses(test_data) as _:
        system_coordinator_mock.data = (
            await system_coordinator_mock._async_update_data()
        )
        manual_cooling = ZoneIsManualCoolingActive(0, 0, system_coordinator_mock)
        assert not manual_cooling.is_on
        await mocked_api.aiohttp_session.close()


async def test_ambisense_device_sensors(
    hass,
    mypyllant_aioresponses,
    mocked_api: MyPyllantAPI,
    system_coordinator_mock: SystemCoordinator,
):
    """
    See https://github.com/signalkraft/mypyllant-component/issues/136
    """
    test_data = load_test_data(DATA_DIR / "ambisense2.yaml")
    with mypyllant_aioresponses(test_data) as _:
        system_coordinator_mock.data = (
            await system_coordinator_mock._async_update_data()
        )
        config_entry = get_config_entry()
        hass.data[DOMAIN][config_entry.entry_id] = {
            "system_coordinator": system_coordinator_mock
        }
        mock = Mock(return_value=None)
        await async_setup_entry(hass, config_entry, mock)
        sensors = mock.call_args.args[0]
        devices = [
            device
            for room in system_coordinator_mock.data[0].ambisense_rooms
            for device in room.room_configuration.devices
        ]
        # Sensors are only created if the device reports the value
        assert len(
            [s for s in sensors if isinstance(s, AmbisenseDeviceLowBattery)]
        ) == len([d for d in devices if d.low_bat is not None])
        assert len(
            [s for s in sensors if isinstance(s, AmbisenseDeviceUnreachable)]
        ) == len([d for d in devices if d.unreach is not None])

        # Sensors reflect new data from the coordinator
        sensor = next(s for s in sensors if isinstance(s, AmbisenseDeviceLowBattery))
        system_coordinator_mock.data = (
            await system_coordinator_mock._async_update_data()
        )
        new_device = next(
            device
            for room in system_coordinator_mock.data[0].ambisense_rooms
            for device in room.room_configuration.devices
            if device.sgtin == sensor.sgtin
        )
        new_device.low_bat = not new_device.low_bat
        assert sensor.is_on == new_device.low_bat
        await mocked_api.aiohttp_session.close()
