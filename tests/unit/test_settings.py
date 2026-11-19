"""Chapter 14 tests: typed configuration, and the mistakes it refuses to accept."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from opcua_toolkit.errors import ConfigurationError, SecurityConfigurationError
from opcua_toolkit.settings import (
    ClientSettings,
    LimitsOverrides,
    RetryPolicy,
    SecuritySettings,
    SubscriptionDefaults,
)

ENDPOINT = "opc.tcp://127.0.0.1:48401"


def test_settings_build_a_client_without_connecting_it() -> None:
    settings = ClientSettings(endpoint_url=ENDPOINT)
    client = settings.create_client()
    assert client.server_url.geturl() == ENDPOINT
    # No connect() here: Chapter 12 owns the lifecycle, not the settings object.
    assert not client.uaclient.has_transport


def test_settings_reject_an_endpoint_that_is_not_opc_tcp() -> None:
    with pytest.raises(ValidationError, match=r"opc\.tcp"):
        ClientSettings(endpoint_url="http://127.0.0.1:4840")


def test_settings_reject_unknown_keys_instead_of_ignoring_a_typo() -> None:
    with pytest.raises(ValidationError):
        ClientSettings(endpoint_url=ENDPOINT, session_timout=12.0)  # type: ignore[call-arg]


def test_settings_require_a_password_when_a_username_is_given() -> None:
    with pytest.raises((ConfigurationError, ValidationError), match="password"):
        ClientSettings(endpoint_url=ENDPOINT, username="operator")


def test_describe_never_prints_the_password() -> None:
    settings = ClientSettings(endpoint_url=ENDPOINT, username="operator", password="s3cret!")
    described = settings.describe()
    assert "s3cret" not in described
    assert "username+password" in described


def test_settings_never_print_key_material() -> None:
    security = SecuritySettings(
        policy="Basic256Sha256",
        mode="SignAndEncrypt",
        certificate=Path("certs/client.pem"),
        private_key=Path("certs/client.key"),
        private_key_password="s3cret!",
    )
    assert "s3cret" not in security.describe()
    assert "Basic256Sha256" in security.describe()


def test_secured_profiles_require_a_certificate_and_a_key() -> None:
    with pytest.raises(SecurityConfigurationError, match="certificate and private_key"):
        SecuritySettings(policy="Basic256Sha256", mode="SignAndEncrypt")
    assert not SecuritySettings().enabled


def test_yaml_roundtrip_keeps_every_setting_identical(tmp_path: Path) -> None:
    settings = ClientSettings(
        endpoint_url=ENDPOINT,
        session_timeout=90.0,
        username="operator",
        password="s3cret",
        subscriptions=SubscriptionDefaults(publishing_interval=250.0, sampling_interval=50.0),
        limits=LimitsOverrides(max_nodes_per_read=50),
    )
    config = tmp_path / "client.yaml"
    config.write_text(settings.to_yaml(), encoding="utf-8")
    assert ClientSettings.from_yaml(config) == settings


def test_from_yaml_reads_a_file(tmp_path: Path) -> None:
    config = tmp_path / "client.yaml"
    config.write_text(f"endpoint_url: {ENDPOINT}\nsession_timeout: 45.0\n", encoding="utf-8")
    settings = ClientSettings.from_yaml(config)
    assert settings.endpoint_url == ENDPOINT
    assert settings.session_timeout == 45.0


def test_from_yaml_rejects_a_file_that_is_not_a_mapping(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="mapping"):
        ClientSettings.from_yaml(broken)


def test_limits_overrides_only_expose_the_limits_a_client_may_cap() -> None:
    overrides = LimitsOverrides(max_nodes_per_read=25, max_nodes_per_browse=5)
    assert overrides.as_dict() == {
        "max_nodes_per_read": 25,
        "max_nodes_per_write": None,
        "max_nodes_per_browse": 5,
        "max_nodes_per_method_call": None,
        "max_monitored_items_per_call": None,
    }


def test_limits_overrides_refuse_zero_and_negative_caps() -> None:
    with pytest.raises(ValidationError):
        LimitsOverrides(max_nodes_per_read=0)


def test_retry_policy_grows_exponentially_and_never_exceeds_its_cap() -> None:
    policy = RetryPolicy(
        max_attempts=5, initial_delay=0.5, multiplier=2.0, max_delay=4.0, jitter=0.0
    )
    delays = [policy.delay_before(attempt) for attempt in range(1, 8)]
    assert delays == [0.5, 1.0, 2.0, 4.0, 4.0, 4.0, 4.0]


def test_retry_policy_jitter_stays_inside_its_own_bound() -> None:
    policy = RetryPolicy(initial_delay=1.0, multiplier=1.0, max_delay=10.0, jitter=0.5)
    for attempt in range(1, 6):
        delay = policy.delay_before(attempt)
        assert 1.0 <= delay <= 1.5


def test_retry_policy_bounds_are_validated() -> None:
    with pytest.raises(ValidationError):
        RetryPolicy(initial_delay=0.0)
    with pytest.raises(ValidationError):
        RetryPolicy(multiplier=0.5)
    with pytest.raises(ValidationError):
        RetryPolicy(jitter=2.0)


def test_retry_policy_counts_attempts_the_way_a_caller_expects() -> None:
    bounded = RetryPolicy(max_attempts=3)
    assert bounded.allows_another(1)
    assert bounded.allows_another(2)
    assert not bounded.allows_another(3)

    unlimited = RetryPolicy(max_attempts=0)
    assert unlimited.allows_another(1)
    assert unlimited.allows_another(10_000)


def test_an_unlimited_retry_policy_is_not_described_as_no_retries() -> None:
    """``max_attempts=0`` means "keep trying", and a log line has to say that.

    Printing it as ``0 attempts`` is the kind of line that sends an engineer to look for
    a retry bug in a gateway that is retrying exactly as configured.
    """
    assert "forever" in RetryPolicy(max_attempts=0).describe()
    assert "3 attempts" in RetryPolicy(max_attempts=3).describe()


def test_an_unsecured_profile_is_described_as_unsecured() -> None:
    """A log line that reads ``None / None`` is indistinguishable from a mistake.

    The names asyncua uses for "no security" are literally ``None``, so the description
    has to translate them into the word an operator would use.
    """
    described = SecuritySettings().describe()
    assert "unsecured" in described
    assert "cert=" not in described


def test_subscription_defaults_are_positive_intervals() -> None:
    defaults = SubscriptionDefaults(publishing_interval=200.0, sampling_interval=0.0, queue_size=1)
    assert defaults.publishing_interval == 200.0
    with pytest.raises(ValidationError):
        SubscriptionDefaults(publishing_interval=0.0)
