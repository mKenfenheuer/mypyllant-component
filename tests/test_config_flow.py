from unittest import mock

import pytest
from aiohttp import RequestInfo
from aiohttp.client_exceptions import ClientResponseError
from homeassistant import data_entry_flow
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from myPyllant.http_client import LoginEndpointInvalid, RealmInvalid

from custom_components.mypyllant.const import DOMAIN
from tests.utils import test_user_input


def _login_endpoint_invalid(status: int) -> LoginEndpointInvalid:
    """
    myPyllant wraps every error response of the login page in LoginEndpointInvalid
    """
    error = LoginEndpointInvalid()
    error.__cause__ = ClientResponseError(
        request_info=RequestInfo(
            url="https://identity.vaillant-group.com/auth/realms/vaillant-germany-b2c/protocol/openid-connect/auth",  # type: ignore
            method="GET",
            headers=None,  # type: ignore
        ),
        history=None,  # type: ignore
        status=status,
        message="Service Unavailable",
    )
    return error


def _config_entry(username: str = "username") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=username,
        unique_id=username,
        data={**test_user_input, "username": username},
    )


async def _submit_user_flow(hass: HomeAssistant, side_effect, user_input=None):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with mock.patch(
        "custom_components.mypyllant.config_flow.validate_input",
        side_effect=side_effect,
    ):
        return await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=user_input or test_user_input,
        )


@pytest.mark.parametrize(
    "error,expected",
    [
        (_login_endpoint_invalid(503), {"base": "cannot_connect"}),
        (_login_endpoint_invalid(404), {"country": "login_endpoint_invalid"}),
        (TimeoutError(), {"base": "cannot_connect"}),
    ],
)
async def test_user_flow_login_errors(hass: HomeAssistant, error, expected):
    """
    A myVAILLANT outage is no invalid login, see
    https://github.com/signalkraft/mypyllant-component/issues/454
    """
    result = await _submit_user_flow(hass, error)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == expected


async def test_user_flow_country_not_supported(hass: HomeAssistant):
    """
    See https://github.com/signalkraft/mypyllant-component/issues/488
    """
    result = await _submit_user_flow(
        hass,
        RealmInvalid(),
        user_input={**test_user_input, "brand": "sdbg", "country": "belgium"},
    )
    assert result["errors"] == {"country": "country_not_supported"}
    assert "Spain" in result["description_placeholders"]["countries"]


async def test_user_flow_already_configured(hass: HomeAssistant):
    _config_entry().add_to_hass(hass)
    with mock.patch(
        "custom_components.mypyllant.config_flow.validate_input",
        return_value="username",
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=test_user_input
        )
    assert result["errors"] == {"base": "already_configured"}


async def test_reconfigure_changes_account(hass: HomeAssistant):
    """
    See https://github.com/signalkraft/mypyllant-component/issues/484
    """
    entry = _config_entry("old@example.com")
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    with (
        mock.patch(
            "custom_components.mypyllant.config_flow.validate_input",
            return_value="new@example.com",
        ),
        mock.patch("custom_components.mypyllant.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={**test_user_input, "username": "new@example.com"},
        )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.title == "new@example.com"
    assert entry.unique_id == "new@example.com"
    assert entry.data["username"] == "new@example.com"


async def test_reauth_with_other_account(hass: HomeAssistant):
    """
    See https://github.com/signalkraft/mypyllant-component/issues/279
    """
    entry = _config_entry("old@example.com")
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    with mock.patch(
        "custom_components.mypyllant.config_flow.validate_input",
        return_value="new@example.com",
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={**test_user_input, "username": "new@example.com"},
        )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"base": "wrong_account"}


async def test_setup_retries_on_login_outage(hass: HomeAssistant):
    """
    A myVAILLANT outage during setup is retried instead of requiring a reauthentication
    """
    entry = _config_entry()
    entry.add_to_hass(hass)
    with mock.patch(
        "custom_components.mypyllant.MyPyllantAPI.login",
        side_effect=_login_endpoint_invalid(503),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
