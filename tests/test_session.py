import asyncio
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from aiohttp import RequestInfo
from aiohttp.client_exceptions import ClientResponseError
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from myPyllant.api import MyPyllantAPI
from myPyllant.http_client import AuthenticationFailed


def _client_response_error(status: int, message: str) -> ClientResponseError:
    return ClientResponseError(
        request_info=RequestInfo(
            url="https://identity.vaillant-group.com/auth/realms/vaillant-germany-b2c/protocol/openid-connect/token",  # type: ignore
            method="POST",
            headers=None,  # type: ignore
        ),
        history=None,  # type: ignore
        status=status,
        message=message,
    )


async def test_concurrent_refresh_session_refreshes_once(
    mocked_api: MyPyllantAPI, system_coordinator_mock
):
    """
    Concurrent commands must not refresh the same refresh token twice, see
    https://github.com/signalkraft/mypyllant-component/issues/497
    """
    calls = 0

    async def refresh_token():
        nonlocal calls
        calls += 1
        # Yield to the event loop, so a concurrent caller would run now without the lock
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        loop.call_soon(future.set_result, None)
        await future
        mocked_api.oauth_session_expires = datetime.now(timezone.utc) + timedelta(
            hours=1
        )

    mocked_api.oauth_session_expires = None
    with mock.patch.object(mocked_api, "refresh_token", side_effect=refresh_token):
        await asyncio.gather(
            system_coordinator_mock._refresh_session(),
            system_coordinator_mock._refresh_session(),
            system_coordinator_mock._refresh_session(),
        )
    assert calls == 1
    await mocked_api.aiohttp_session.close()


async def test_refresh_session_logs_in_on_invalid_grant(
    mocked_api: MyPyllantAPI, system_coordinator_mock
):
    mocked_api.oauth_session_expires = None
    invalid_grant = _client_response_error(
        400,
        'Bad Request, response was: {"error":"invalid_grant","error_description":"Session doesn\'t have required client"}',
    )
    with (
        mock.patch.object(mocked_api, "refresh_token", side_effect=invalid_grant),
        mock.patch.object(mocked_api, "login") as login,
    ):
        await system_coordinator_mock._refresh_session()
    login.assert_awaited_once()
    await mocked_api.aiohttp_session.close()


async def test_refresh_session_raises_auth_failed_when_login_fails(
    mocked_api: MyPyllantAPI, system_coordinator_mock
):
    mocked_api.oauth_session_expires = None
    with (
        mock.patch.object(
            mocked_api,
            "refresh_token",
            side_effect=_client_response_error(400, "invalid_grant"),
        ),
        mock.patch.object(
            mocked_api, "login", side_effect=AuthenticationFailed("Login failed")
        ),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await system_coordinator_mock._refresh_session()
    await mocked_api.aiohttp_session.close()


async def test_refresh_session_reraises_other_errors(
    mocked_api: MyPyllantAPI, system_coordinator_mock
):
    mocked_api.oauth_session_expires = None
    with (
        mock.patch.object(
            mocked_api,
            "refresh_token",
            side_effect=_client_response_error(503, "Service Unavailable"),
        ),
        mock.patch.object(mocked_api, "login") as login,
        pytest.raises(ClientResponseError),
    ):
        await system_coordinator_mock._refresh_session()
    login.assert_not_awaited()
    await mocked_api.aiohttp_session.close()


async def test_unauthorized_response_invalidates_session(
    mypyllant_aioresponses, mocked_api: MyPyllantAPI, system_coordinator_mock
):
    mocked_api.oauth_session_expires = datetime.now(timezone.utc) + timedelta(hours=1)
    with mypyllant_aioresponses(
        raise_exception=_client_response_error(401, "Unauthorized")
    ) as _:
        with pytest.raises(UpdateFailed):
            await system_coordinator_mock._async_update_data()
    assert mocked_api.oauth_session_expires is None
    await mocked_api.aiohttp_session.close()
