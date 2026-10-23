from __future__ import annotations

import logging
from datetime import datetime as dt, timedelta, timezone
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    SupportsResponse,
    ServiceCall,
    ServiceResponse,
    callback,
)
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import selector
from homeassistant.helpers.event import async_track_time_change

from myPyllant import export, report

from myPyllant.api import MyPyllantAPI
from myPyllant.const import DEFAULT_BRAND
from myPyllant.enums import DeviceDataBucketResolution
from myPyllant.http_client import (
    AuthenticationFailed,
    RealmInvalid,
    LoginEndpointInvalid,
)
from myPyllant.tests import generate_test_data
from .const import (
    API_DOWN_PAUSE_INTERVAL,
    DEFAULT_COUNTRY,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    OPTION_BRAND,
    OPTION_COUNTRY,
    OPTION_UPDATE_INTERVAL,
    SERVICE_GENERATE_TEST_DATA,
    SERVICE_EXPORT,
    SERVICE_REPORT,
    OPTION_UPDATE_INTERVAL_DAILY,
    DEFAULT_UPDATE_INTERVAL_DAILY,
)
from .coordinator import SystemCoordinator, DailyDataCoordinator
from .utils import RedactSecretsFilter, is_transient_login_error

_LOGGER = logging.getLogger(__name__)

_REDACT_SECRETS_FILTER = RedactSecretsFilter()
for _logger_name in ("myPyllant.api", "myPyllant.http_client"):
    logging.getLogger(_logger_name).addFilter(_REDACT_SECRETS_FILTER)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CALENDAR,
    Platform.CLIMATE,
    Platform.DATETIME,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.WATER_HEATER,
]

_DEVICE_DATA_BUCKET_RESOLUTION_OPTIONS = [
    selector.SelectOptionDict(value=v.value, label=v.value.title())
    for v in DeviceDataBucketResolution
]


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s", config_entry.version)

    if config_entry.version == 1:
        """
        from homeassistant.helpers.entity_registry import async_migrate_entries, RegistryEntry
        from homeassistant.helpers.device_registry import async_entries_for_config_entry
        from homeassistant.core import callback

        devices = async_entries_for_config_entry(
            hass.data["device_registry"], config_entry.entry_id
        )

        @callback
        def update_unique_id(entity_entry: RegistryEntry):
            return {"new_unique_id": entity_entry.unique_id} # change entity_entry.unique_id

        await async_migrate_entries(hass, config_entry.entry_id, update_unique_id)
        config_entry.version = 2 # set to new version
        """

    _LOGGER.debug("Migration to version %s successful", config_entry.version)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    username: str = entry.data.get("username")  # type: ignore
    password: str = entry.data.get("password")  # type: ignore
    update_interval = entry.options.get(OPTION_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
    update_interval_daily = entry.options.get(
        OPTION_UPDATE_INTERVAL_DAILY, DEFAULT_UPDATE_INTERVAL_DAILY
    )
    country = entry.options.get(
        OPTION_COUNTRY, entry.data.get(OPTION_COUNTRY, DEFAULT_COUNTRY)
    )
    brand = entry.options.get(OPTION_BRAND, entry.data.get(OPTION_BRAND, DEFAULT_BRAND))

    hass.data.setdefault(DOMAIN, {})
    # Keep quota state from a previous setup attempt (ConfigEntryNotReady), so retries respect the quota
    hass.data[DOMAIN].setdefault(entry.entry_id, {})

    _LOGGER.debug("Creating API and logging in with %s in realm %s", username, country)
    api = MyPyllantAPI(
        username=username, password=password, brand=brand, country=country
    )
    try:
        await api.login()
    except (AuthenticationFailed, LoginEndpointInvalid, RealmInvalid) as e:
        await api.aiohttp_session.close()
        if is_transient_login_error(e):
            raise ConfigEntryNotReady(
                f"myVAILLANT login is currently not available: {e.__cause__}"
            ) from e
        raise ConfigEntryAuthFailed from e
    except Exception as e:
        # i.e. network errors or error responses of the token endpoint during outages
        await api.aiohttp_session.close()
        raise ConfigEntryNotReady(f"Could not log in to myVAILLANT: {e}") from e

    system_coordinator = SystemCoordinator(
        hass, api, entry, timedelta(seconds=update_interval)
    )
    _LOGGER.debug("Refreshing SystemCoordinator")
    try:
        # Raises ConfigEntryNotReady if the first fetch fails (i.e. quota exceeded), so Home Assistant
        # retries the setup. Otherwise the platforms would be set up without data and never create entities
        await system_coordinator.async_config_entry_first_refresh()
    except Exception:
        await api.aiohttp_session.close()
        raise
    hass.data[DOMAIN][entry.entry_id]["system_coordinator"] = system_coordinator

    # Daily data coordinator is fetched once by default (to get all entities), and then only once per night
    # to prevent quota errors
    daily_data_coordinator = DailyDataCoordinator(
        hass,
        api,
        entry,
        timedelta(seconds=update_interval_daily) if update_interval_daily else None,
    )
    _LOGGER.debug("Refreshing DailyDataCoordinator")
    await daily_data_coordinator.async_refresh()
    hass.data[DOMAIN][entry.entry_id]["daily_data_coordinator"] = daily_data_coordinator

    last_daily_data_retry: dt | None = None

    @callback
    def _refresh_missing_daily_data() -> None:
        # Daily data failed during setup (i.e. quota exceeded) and isn't polled without an update interval,
        # so it's retried after successful system updates, at most every API_DOWN_PAUSE_INTERVAL
        nonlocal last_daily_data_retry
        if not system_coordinator.last_update_success or daily_data_coordinator.data:
            return
        now = dt.now(timezone.utc)
        if last_daily_data_retry is None or now - last_daily_data_retry > timedelta(
            seconds=API_DOWN_PAUSE_INTERVAL
        ):
            last_daily_data_retry = now
            entry.async_create_background_task(
                hass,
                daily_data_coordinator.async_request_refresh(),
                "mypyllant_refresh_missing_daily_data",
            )

    entry.async_on_unload(
        system_coordinator.async_add_listener(_refresh_missing_daily_data)
    )

    if not update_interval_daily:
        # Without an update interval, energy data is fetched once per night, so the hourly statistics
        # of the previous day are complete and the sensors don't stay at 0 after midnight
        async def _refresh_daily_data(now: dt) -> None:
            await daily_data_coordinator.async_request_refresh()

        entry.async_on_unload(
            async_track_time_change(
                hass, _refresh_daily_data, hour=0, minute=30, second=0
            )
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def handle_export(call: ServiceCall) -> ServiceResponse:
        _LOGGER.debug("Exporting data with params %s", call.data)
        return {
            "export": await export.main(
                user=username,
                password=password,
                brand=brand,
                country=country,
                data=call.data.get("data", False),
                resolution=call.data.get("resolution", DeviceDataBucketResolution.DAY),
                start=call.data.get("start"),
                end=call.data.get("end"),
            )
        }

    async def handle_generate_test_data(call: ServiceCall) -> ServiceResponse:
        return await generate_test_data.main(
            user=username,
            password=password,
            brand=brand,
            country=country,
            write_results=False,
        )

    async def handle_report(call: ServiceCall) -> ServiceResponse:
        return {
            f.file_name: f.file_content
            for f in await report.main(
                user=username,
                password=password,
                brand=brand,
                country=country,
                year=int(call.data.get("year", dt.now().year)),
                write_results=False,
            )
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT,
        handle_export,
        schema=vol.Schema(
            {
                vol.Optional("data"): bool,
                vol.Optional("resolution"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=_DEVICE_DATA_BUCKET_RESOLUTION_OPTIONS,
                        mode=selector.SelectSelectorMode.LIST,
                    ),
                ),
                vol.Optional("start"): vol.Coerce(dt.fromisoformat),
                vol.Optional("end"): vol.Coerce(dt.fromisoformat),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GENERATE_TEST_DATA,
        handle_generate_test_data,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REPORT,
        handle_report,
        schema=vol.Schema(
            {
                vol.Required("year", default=dt.now().year): vol.Coerce(int),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await hass.data[DOMAIN][entry.entry_id][
            "system_coordinator"
        ].api.aiohttp_session.close()
        await hass.data[DOMAIN][entry.entry_id][
            "daily_data_coordinator"
        ].api.aiohttp_session.close()
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
