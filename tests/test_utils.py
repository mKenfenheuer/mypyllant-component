import logging

import pytest

from custom_components.mypyllant.utils import EntityList, RedactSecretsFilter


async def test_log_on_entity_exception(caplog):
    def _raise():
        raise Exception("Init Exception")

    entities = EntityList()
    entities.append(lambda: dict(test=1))
    with caplog.at_level(logging.ERROR):
        entities.append(_raise)
    entities.append([0])
    assert len(entities) == 2
    assert "test" in entities[0]
    assert 0 in entities[1]


@pytest.mark.parametrize(
    "args,expected",
    [
        (
            ("POST", "https://login", b"username=me&password=s3cr3t&credentialId="),
            "Sending POST to https://login with b'username=me&password=***&credentialId='",
        ),
        (
            ("POST", "https://token", b"refresh_token=eyJabc.def&client_id=x"),
            "Sending POST to https://token with b'refresh_token=***&client_id=x'",
        ),
        (
            ("GET", "https://api", {"access_token": "eyJabc", "expires_in": 300}),
            "Sending GET to https://api with {'access_token': '***', 'expires_in': 300}",
        ),
        (
            ("GET", "https://api", "temperature=21"),
            "Sending GET to https://api with temperature=21",
        ),
    ],
)
def test_redact_secrets_filter(args, expected):
    """
    myPyllant logs passwords and tokens in its debug logs
    """
    record = logging.LogRecord(
        "myPyllant.http_client",
        logging.DEBUG,
        __file__,
        1,
        "Sending %s to %s with %s",
        args,
        None,
    )
    assert RedactSecretsFilter().filter(record)
    assert record.getMessage() == expected
