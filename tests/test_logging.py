"""Logging: secrets must never reach a log record."""

import logging

import pytest

from app.utils.logger import SecretScrubber, get_logger, setup_logging


@pytest.fixture
def capture():
    """Capture what a real log record turns into, after the scrubber runs."""
    records: list[str] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("rpmstream.test.scrub")
    logger.setLevel(logging.DEBUG)
    handler = Collector()
    handler.addFilter(SecretScrubber())
    logger.addHandler(handler)
    logger.propagate = False
    yield records
    logger.removeHandler(handler)


def test_api_key_in_a_query_string_is_scrubbed(capture):
    logging.getLogger("rpmstream.test.scrub").info("GET https://rpmshare.com/api/account/info?key=45bjlf82v0aqaigno")
    assert "45bjlf82v0aqaigno" not in capture[0]
    assert "key=***" in capture[0]


def test_assignment_style_secrets_are_scrubbed(capture):
    log = logging.getLogger("rpmstream.test.scrub")
    log.info("connecting with api_key=SUPERSECRET123 and token=123456:AAFake")
    assert "SUPERSECRET123" not in capture[0]
    assert "AAFake" not in capture[0]


def test_plain_messages_are_untouched(capture):
    log = logging.getLogger("rpmstream.test.scrub")
    log.info("Uploading Movie.mp4 (1048576 bytes) as job abcd1234")
    assert capture[0] == "Uploading Movie.mp4 (1048576 bytes) as job abcd1234"


def test_safe_repr_and_scrubber_together(settings, capture):
    """Belt and braces: even the startup summary line stays clean."""
    log = logging.getLogger("rpmstream.test.scrub")
    log.info("Settings → %s", settings.safe_repr())
    assert settings.rpmshare_api_key not in capture[0]
    assert settings.bot_token not in capture[0]


def test_get_logger_is_namespaced():
    logger = get_logger("unit")
    assert logger.name == "rpmstream.unit"
    # calling setup_logging twice must not duplicate handlers
    before = len(logging.getLogger().handlers)
    setup_logging("INFO", log_file=None, to_file=False)
    setup_logging("INFO", log_file=None, to_file=False)
    assert len(logging.getLogger().handlers) == before


def test_implicit_setup_writes_no_file_but_explicit_does(tmp_path):
    """Importing the app must not create files; main() still gets its log file."""
    import logging as _logging

    import app.utils.logger as logmod

    root = _logging.getLogger()
    saved_handlers = list(logmod._OUR_HANDLERS)
    saved_state = (logmod._CONFIGURED, logmod._IMPLICIT)
    try:
        logmod._CONFIGURED = False
        logmod._IMPLICIT = False
        logmod._OUR_HANDLERS.clear()

        logmod.get_logger("implicit").info("imported the app")
        assert not (tmp_path / "rpmstream.log").exists(), "an import must not create a log file"

        target = tmp_path / "rpmstream.log"
        logmod.setup_logging("INFO", str(target), True)
        logmod.get_logger("explicit").info("Uploading with key=SECRETVALUE123")
        for handler in root.handlers:
            handler.flush()

        assert target.exists(), "the explicit configuration must create the log file"
        content = target.read_text()
        assert "SECRETVALUE123" not in content, "secrets must never reach the log file"
        assert "key=***" in content
    finally:
        for handler in list(logmod._OUR_HANDLERS):
            root.removeHandler(handler)
            handler.close()
        logmod._OUR_HANDLERS.clear()
        for handler in saved_handlers:
            root.addHandler(handler)
            logmod._OUR_HANDLERS.append(handler)
        logmod._CONFIGURED, logmod._IMPLICIT = saved_state
