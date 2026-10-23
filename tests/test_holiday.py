from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time

from custom_components.mypyllant.const import OPTION_DEFAULT_HOLIDAY_SETPOINT
from custom_components.mypyllant.switch import SystemHolidaySwitch
from custom_components.mypyllant.utils import (
    set_cooling_for_days,
    set_holiday,
    to_api_datetime,
)
from myPyllant.utils import datetime_format
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mypyllant.const import DOMAIN

BERLIN = ZoneInfo("Europe/Berlin")


def _mock_system(is_vrc700=False):
    system = MagicMock()
    system.timezone = BERLIN
    system.control_identifier.is_vrc700 = is_vrc700
    return system


def test_to_api_datetime():
    # 17:40 in CEST is 15:40 in UTC
    local = datetime(2026, 7, 7, 17, 40, 47, tzinfo=BERLIN)
    assert to_api_datetime(local, BERLIN) == datetime(
        2026, 7, 7, 15, 40, 47, tzinfo=timezone.utc
    )
    # Naive datetimes are interpreted in the timezone of the system
    assert to_api_datetime(datetime(2026, 7, 7, 17, 40, 47), BERLIN) == datetime(
        2026, 7, 7, 15, 40, 47, tzinfo=timezone.utc
    )
    assert to_api_datetime(None, BERLIN) is None
    # The library appends "Z", which is only correct for UTC
    assert (
        datetime_format(to_api_datetime(local, BERLIN), with_microseconds=True)
        == "2026-07-07T15:40:47.000Z"
    )


@freeze_time("2026-07-07 15:40:47+00:00")
async def test_set_holiday_defaults_to_utc_now():
    """
    See https://github.com/signalkraft/mypyllant-component/issues/501
    """
    api = MagicMock()
    api.set_holiday = AsyncMock()
    system = _mock_system()
    await set_holiday(api, system, default_duration=3)
    start = datetime(2026, 7, 7, 15, 40, 47, tzinfo=timezone.utc)
    api.set_holiday.assert_awaited_once_with(
        system, start=start, end=start + timedelta(days=3), setpoint=None
    )


async def test_set_holiday_converts_to_utc():
    api = MagicMock()
    api.set_holiday = AsyncMock()
    system = _mock_system(is_vrc700=True)
    await set_holiday(
        api,
        system,
        start=datetime(2026, 1, 10, 8, 0),
        end=datetime(2026, 1, 20, 18, 0, tzinfo=BERLIN),
        setpoint=15.0,
    )
    api.set_holiday.assert_awaited_once_with(
        system,
        start=datetime(2026, 1, 10, 7, 0, tzinfo=timezone.utc),
        end=datetime(2026, 1, 20, 17, 0, tzinfo=timezone.utc),
        setpoint=15.0,
    )


@freeze_time("2026-07-07 15:40:47+00:00")
async def test_set_cooling_for_days_converts_to_utc():
    api = MagicMock()
    api.set_cooling_for_days = AsyncMock()
    system = _mock_system()
    await set_cooling_for_days(api, system, duration_days=2)
    start = datetime(2026, 7, 7, 15, 40, 47, tzinfo=timezone.utc)
    api.set_cooling_for_days.assert_awaited_once_with(
        system, start=start, end=start + timedelta(days=2)
    )

    with pytest.raises(ValueError):
        await set_cooling_for_days(api, system, start=start, duration_days=2)


async def test_set_cooling_for_days_vrc700_passes_duration():
    api = MagicMock()
    api.set_cooling_for_days = AsyncMock()
    system = _mock_system(is_vrc700=True)
    await set_cooling_for_days(api, system, duration_days=2)
    api.set_cooling_for_days.assert_awaited_once_with(system, None, None, 2)


@pytest.mark.parametrize("is_vrc700,expected", [(True, 17.0), (False, None)])
async def test_holiday_switch_uses_default_setpoint_option(is_vrc700, expected):
    """
    See https://github.com/signalkraft/mypyllant-component/issues/409
    """
    system = _mock_system(is_vrc700=is_vrc700)
    system.zones = []
    coordinator = MagicMock()
    coordinator.data = [system]
    coordinator.api.set_holiday = AsyncMock()
    coordinator._refresh_session = AsyncMock()
    coordinator.async_request_refresh_delayed = AsyncMock()
    entry = MockConfigEntry(
        domain=DOMAIN, options={OPTION_DEFAULT_HOLIDAY_SETPOINT: 17.0}
    )
    switch = SystemHolidaySwitch(0, coordinator, entry)
    await switch.async_turn_on()
    assert coordinator.api.set_holiday.await_args.kwargs["setpoint"] == expected
    assert coordinator.api.set_holiday.await_args.kwargs["start"].tzinfo == timezone.utc
