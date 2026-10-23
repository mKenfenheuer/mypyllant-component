"""Tests for pyscript config flow."""

import logging
from datetime import timedelta
from unittest import mock

from homeassistant import data_entry_flow
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from myPyllant.api import MyPyllantAPI
from myPyllant.http_client import AuthenticationFailed
from myPyllant.tests.generate_test_data import DATA_DIR
from myPyllant.tests.utils import load_test_data

from custom_components.mypyllant.const import DOMAIN
from custom_components.mypyllant.config_flow import DATA_SCHEMA
from tests.utils import get_config_entry, test_user_input

_LOGGER = logging.getLogger(__name__)


async def test_flow_init(hass):
    """Test the initial flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    expected = {
        "data_schema": DATA_SCHEMA,
        "description_placeholders": None,
        "errors": {},
        "flow_id": mock.ANY,
        "handler": "mypyllant",
        "step_id": "user",
        "type": "form",
        "last_step": None,
        "preview": None,
    }
    assert expected == result


async def test_user_flow_minimum_fields(hass: HomeAssistant):
    """Test user config flow with minimum fields."""
    # test form shows
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"

    with mock.patch(
        "custom_components.mypyllant.config_flow.validate_input",
        side_effect=AuthenticationFailed,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=test_user_input,
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM


async def test_async_setup(
    hass,
    mypyllant_aioresponses,
    mocked_api: MyPyllantAPI,
):
    test_data = load_test_data(DATA_DIR / "heatpump_heat_curve")
    with mypyllant_aioresponses(test_data) as _:
        config_entry = get_config_entry()
        config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

        assert config_entry.state is ConfigEntryState.LOADED, (
            "Component did not setup successfully"
        )

        result = await hass.config_entries.async_unload(config_entry.entry_id)
        assert result, "Component did not unload successfully"

    await mocked_api.aiohttp_session.close()


async def test_async_setup_retries_without_system_data(
    hass,
    mypyllant_aioresponses,
    mocked_api: MyPyllantAPI,
):
    """
    If the first system fetch fails (i.e. quota exceeded), setup is retried instead of
    loading without entities, see https://github.com/signalkraft/mypyllant-component/issues/510
    """
    test_data = load_test_data(DATA_DIR / "heatpump_heat_curve")
    with (
        mypyllant_aioresponses(test_data) as _,
        mock.patch(
            "custom_components.mypyllant.SystemCoordinator._async_update_data",
            side_effect=UpdateFailed("Quota Exceeded"),
        ),
    ):
        config_entry = get_config_entry()
        config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        assert config_entry.state is ConfigEntryState.SETUP_RETRY

    await mocked_api.aiohttp_session.close()


async def test_missing_daily_data_is_refetched(
    hass,
    mypyllant_aioresponses,
    mocked_api: MyPyllantAPI,
):
    """
    If daily data couldn't be fetched during setup, it's refetched after the next system update,
    even without an update interval for daily data
    """
    test_data = load_test_data(DATA_DIR / "heatpump_heat_curve")
    with mypyllant_aioresponses(test_data) as _:
        with mock.patch(
            "custom_components.mypyllant.DailyDataCoordinator._async_update_data",
            side_effect=UpdateFailed("Quota Exceeded"),
        ):
            config_entry = get_config_entry()
            config_entry.add_to_hass(hass)
            await hass.config_entries.async_setup(config_entry.entry_id)
            await hass.async_block_till_done()
        hass_data = hass.data[DOMAIN][config_entry.entry_id]
        daily_data_coordinator = hass_data["daily_data_coordinator"]
        assert not daily_data_coordinator.data

        with mock.patch.object(
            daily_data_coordinator, "async_request_refresh"
        ) as refresh:
            await hass_data["system_coordinator"].async_refresh()
            await hass.async_block_till_done()
            # Retries are throttled, i.e. when data is updated after changing a setting
            await hass_data["system_coordinator"].async_refresh()
            await hass.async_block_till_done()
        refresh.assert_awaited_once()

        await hass.config_entries.async_unload(config_entry.entry_id)
    await mocked_api.aiohttp_session.close()


async def test_daily_data_refreshed_at_night(
    hass,
    mypyllant_aioresponses,
    mocked_api: MyPyllantAPI,
):
    test_data = load_test_data(DATA_DIR / "heatpump_heat_curve")
    with mypyllant_aioresponses(test_data) as _:
        config_entry = get_config_entry()
        config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        daily_data_coordinator = hass.data[DOMAIN][config_entry.entry_id][
            "daily_data_coordinator"
        ]

        with mock.patch.object(
            daily_data_coordinator, "async_request_refresh"
        ) as refresh:
            async_fire_time_changed(
                hass,
                (dt_util.now() + timedelta(days=1)).replace(
                    hour=0, minute=30, second=0, microsecond=0
                ),
            )
            await hass.async_block_till_done()
        refresh.assert_awaited_once()

        await hass.config_entries.async_unload(config_entry.entry_id)
    await mocked_api.aiohttp_session.close()
