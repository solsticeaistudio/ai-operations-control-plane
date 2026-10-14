import pytest

from app.config import Settings, validate_settings


def _settings(**overrides):
    base = {
        "aiops_env": "production",
        "api_key": "s3cret-key",
        "demo_mode": False,
        "allow_storage_fallback": False,
    }
    base.update(overrides)
    return Settings(**base)


def test_development_defaults_are_allowed():
    # Local/demo configuration must keep working (insecure but explicit).
    s = Settings(aiops_env="development", api_key="", demo_mode=True, allow_storage_fallback=True)
    validate_settings(s)  # must not raise


def test_production_safe_config_is_allowed():
    validate_settings(_settings())


def test_production_requires_api_key():
    with pytest.raises(RuntimeError) as exc:
        validate_settings(_settings(api_key=""))
    assert "AIOPS_API_KEY" in str(exc.value)


def test_production_forbids_demo_mode():
    with pytest.raises(RuntimeError) as exc:
        validate_settings(_settings(demo_mode=True))
    assert "AIOPS_DEMO_MODE" in str(exc.value)


def test_production_forbids_storage_fallback():
    with pytest.raises(RuntimeError) as exc:
        validate_settings(_settings(allow_storage_fallback=True))
    assert "ALLOW_STORAGE_FALLBACK" in str(exc.value)


def test_production_reports_all_problems_at_once():
    with pytest.raises(RuntimeError) as exc:
        validate_settings(_settings(api_key="", demo_mode=True, allow_storage_fallback=True))
    message = str(exc.value)
    assert "AIOPS_API_KEY" in message
    assert "AIOPS_DEMO_MODE" in message
    assert "ALLOW_STORAGE_FALLBACK" in message


def test_env_name_is_case_insensitive():
    validate_settings(Settings(aiops_env="PRODUCTION", api_key="k", demo_mode=False, allow_storage_fallback=False))
    with pytest.raises(RuntimeError):
        validate_settings(Settings(aiops_env="Production", api_key="", demo_mode=False, allow_storage_fallback=False))


def test_fail_closed_message_points_to_docs():
    with pytest.raises(RuntimeError) as exc:
        validate_settings(_settings(api_key=""))
    assert "SECURITY.md" in str(exc.value) or ".env.production.example" in str(exc.value)
