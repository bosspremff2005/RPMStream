"""Environment configuration."""

import pytest

from app.config.settings import MAX_CHUNK_SIZE, MIN_CHUNK_SIZE, ConfigError, Settings


def test_loads_from_env(env):
    settings = Settings.from_env(env=env)
    assert settings.api_id == 123456
    assert settings.api_hash.startswith("0123")
    assert settings.bot_token == "123456:TEST-token"
    assert settings.rpmshare_api_key == "test-api-key"
    assert settings.max_concurrent_uploads == 1
    assert settings.chunk_size == MAX_CHUNK_SIZE
    assert settings.max_retries == 3
    assert settings.log_level == "INFO"


def test_missing_secrets_are_reported_together():
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env(env={})
    message = str(excinfo.value)
    for key in ("API_ID", "API_HASH", "BOT_TOKEN", "RPMSHARE_API_KEY"):
        assert key in message


def test_chunk_size_is_clamped_and_aligned(env):
    env["CHUNK_SIZE"] = "999999999"
    assert Settings.from_env(env=env).chunk_size == MAX_CHUNK_SIZE

    env["CHUNK_SIZE"] = "1"
    assert Settings.from_env(env=env).chunk_size == MIN_CHUNK_SIZE

    env["CHUNK_SIZE"] = "500001"  # not a multiple of 4096
    assert Settings.from_env(env=env).chunk_size % 4096 == 0


def test_booleans_and_lists(env):
    env["RPMSHARE_FILE_PUBLIC"] = "no"
    env["RPMSHARE_FILE_ADULT"] = "1"
    env["ALLOWED_USERS"] = "111, 222 ;333"
    env["LOG_TO_FILE"] = "false"
    settings = Settings.from_env(env=env)
    assert settings.rpmshare_public is False
    assert settings.rpmshare_adult is True
    assert settings.allowed_users == (111, 222, 333)
    assert settings.log_to_file is False
    assert settings.user_allowed(222) is True
    assert settings.user_allowed(999) is False


def test_public_bot_allows_everyone(env):
    settings = Settings.from_env(env=env)
    assert settings.user_allowed(12345) is True
    assert settings.user_allowed(None) is True


def test_validation_rejects_bad_values(env):
    env["PROGRESS_UPDATE_INTERVAL"] = "0.2"
    with pytest.raises(ConfigError, match="PROGRESS_UPDATE_INTERVAL"):
        Settings.from_env(env=env)

    env = dict(env, PROGRESS_UPDATE_INTERVAL="3")
    env["RPMSHARE_FILE_URL_TEMPLATE"] = "https://rpmshare.com/no-placeholder"
    with pytest.raises(ConfigError, match="file_code"):
        Settings.from_env(env=env)

    env = dict(env, RPMSHARE_FILE_URL_TEMPLATE="https://rpmshare.com/{file_code}")
    env["LOG_LEVEL"] = "VERBOSE"
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        Settings.from_env(env=env)

    env = dict(env, LOG_LEVEL="debug")
    env["ALLOWED_USERS"] = "not-a-number"
    with pytest.raises(ConfigError, match="ALLOWED_USERS"):
        Settings.from_env(env=env)


def test_optional_ids_are_parsed(env):
    env["RPMSHARE_FOLDER_ID"] = "25"
    env["RPMSHARE_CATEGORY_ID"] = "5"
    env["MAX_FILE_SIZE_MB"] = "20480"
    settings = Settings.from_env(env=env)
    assert settings.rpmshare_folder_id == 25
    assert settings.rpmshare_category_id == 5
    assert settings.max_file_size_mb == 20480


def test_safe_repr_never_leaks_secrets(env):
    text = Settings.from_env(env=env).safe_repr()
    assert "test-api-key" not in text
    assert "123456:TEST-token" not in text
    assert env["API_HASH"] not in text
    assert "rpmshare_api_key" in text
