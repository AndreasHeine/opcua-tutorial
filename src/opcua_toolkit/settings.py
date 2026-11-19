"""Typed, validated client configuration.

One place that knows how to turn configuration into an asyncua ``Client``.
Every chapter that needs a connection gets it from
:meth:`ClientSettings.create_client`, so timeouts, security and retry policy
are never scattered across examples - and never silently different between the
tutorial code and the final project code.
"""

from __future__ import annotations

import random
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from asyncua import Client, ua
from asyncua.crypto import security_policies
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from opcua_toolkit.errors import ConfigurationError, SecurityConfigurationError

SecurityPolicyName = Literal["None", "Basic256Sha256", "Aes128Sha256RsaOaep", "Aes256Sha256RsaPss"]
SecurityModeName = Literal["None", "Sign", "SignAndEncrypt"]

_POLICY_CLASSES: dict[str, type[security_policies.SecurityPolicy]] = {
    "None": security_policies.SecurityPolicyNone,
    "Basic256Sha256": security_policies.SecurityPolicyBasic256Sha256,
    "Aes128Sha256RsaOaep": security_policies.SecurityPolicyAes128Sha256RsaOaep,
    "Aes256Sha256RsaPss": security_policies.SecurityPolicyAes256Sha256RsaPss,
}

_MODE_CLASSES: dict[str, ua.MessageSecurityMode] = {
    "None": ua.MessageSecurityMode.None_,
    "Sign": ua.MessageSecurityMode.Sign,
    "SignAndEncrypt": ua.MessageSecurityMode.SignAndEncrypt,
}

#: Recommended production defaults. None/Aes128Sha256RsaOaep are the modern
#: policies; Basic256Sha256 is the one that still talks to older plants.
RECOMMENDED_POLICIES: tuple[SecurityPolicyName, ...] = (
    "Aes256Sha256RsaPss",
    "Aes128Sha256RsaOaep",
    "Basic256Sha256",
)

#: Everything a client of this tutorial can actually speak, secured or not. A server may
#: publish a policy outside this set - ``Basic128Rsa15`` is the usual one - and the answer
#: has to be "this client does not speak that", not a quieter connection nobody asked for.
SUPPORTED_POLICIES: tuple[SecurityPolicyName, ...] = ("None", *RECOMMENDED_POLICIES)


class RetryPolicy(BaseModel):
    """Exponential backoff with jitter, shared by reconnect and write retries."""

    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(default=0, ge=0, description="0 means retry forever")
    initial_delay: float = Field(default=0.5, gt=0.0)
    max_delay: float = Field(default=30.0, gt=0.0)
    multiplier: float = Field(default=2.0, ge=1.0)
    jitter: float = Field(default=0.2, ge=0.0, le=1.0, description="Fraction added to each delay")

    def delay_before(self, attempt: int) -> float:
        """Seconds to wait before retry number ``attempt`` (1 based)."""
        base = min(self.initial_delay * (self.multiplier ** (attempt - 1)), self.max_delay)
        noise = base * self.jitter * random.random()
        return min(base + noise, self.max_delay)

    def allows_another(self, attempt: int) -> bool:
        """Whether a retry with index ``attempt`` is permitted."""
        return self.max_attempts == 0 or attempt < self.max_attempts

    def describe(self) -> str:
        """How this policy reads in a log line.

        ``max_attempts=0`` means "keep trying", and printing it as ``0 attempts`` makes a
        gateway that never gives up look like one that never retries at all - which is the
        kind of line that gets an engineer chasing the wrong bug.
        """
        limit = "forever" if self.max_attempts == 0 else f"{self.max_attempts} attempts"
        return f"{limit}, delay capped at {self.max_delay}s"


class SecuritySettings(BaseModel):
    """Application certificate + policy selection for one endpoint."""

    model_config = ConfigDict(frozen=True)

    policy: SecurityPolicyName = "None"
    mode: SecurityModeName = "None"
    certificate: Path | None = None
    private_key: Path | None = None
    private_key_password: str | None = None
    server_certificate: Path | None = None

    @property
    def enabled(self) -> bool:
        """True when the endpoint should not be plain ``SecurityPolicyNone``."""
        return self.policy != "None" or self.mode != "None"

    @model_validator(mode="after")
    def _require_material_for_secured_channels(self) -> SecuritySettings:
        if self.enabled and (self.certificate is None or self.private_key is None):
            raise SecurityConfigurationError(
                f"policy={self.policy} mode={self.mode} requires certificate and private_key"
            )
        return self

    def apply(self, client: Client) -> None:
        """Configure the asyncua client for this security profile."""
        if not self.enabled:
            return
        policy_class = _POLICY_CLASSES[self.policy]
        client.set_security(
            policy_class,
            certificate=self.certificate,
            private_key=self.private_key,
            private_key_password=self.private_key_password,
            server_certificate=self.server_certificate,
            mode=_MODE_CLASSES[self.mode],
        )

    def describe(self) -> str:
        """Safe one line description that never prints key material.

        The unsecured case says what it means instead of echoing the two asyncua names
        that represent it: ``None`` in a log line is indistinguishable from a mistake.
        """
        if not self.enabled:
            return "unsecured (policy None, mode None)"
        return f"{self.policy} / {self.mode} (cert={self.certificate})"


class SubscriptionDefaults(BaseModel):
    """Defaults applied when a monitored item does not override them."""

    model_config = ConfigDict(frozen=True)

    publishing_interval: float = Field(default=500.0, gt=0.0, description="ms between publishes")
    sampling_interval: float = Field(default=100.0, ge=0.0, description="ms, 0 = server driven")
    queue_size: int = Field(default=10, ge=0)


class LimitsOverrides(BaseModel):
    """Optional client-side caps, applied on top of what the server publishes."""

    model_config = ConfigDict(frozen=True)

    max_nodes_per_read: int | None = Field(default=None, gt=0)
    max_nodes_per_write: int | None = Field(default=None, gt=0)
    max_nodes_per_browse: int | None = Field(default=None, gt=0)
    max_nodes_per_method_call: int | None = Field(default=None, gt=0)
    max_monitored_items_per_call: int | None = Field(default=None, gt=0)

    def as_dict(self) -> dict[str, int | None]:
        """Plain mapping consumable by :meth:`OperationLimits.with_overrides`."""
        return {
            "max_nodes_per_read": self.max_nodes_per_read,
            "max_nodes_per_write": self.max_nodes_per_write,
            "max_nodes_per_browse": self.max_nodes_per_browse,
            "max_nodes_per_method_call": self.max_nodes_per_method_call,
            "max_monitored_items_per_call": self.max_monitored_items_per_call,
        }


class ConnectionState(StrEnum):
    """Toolkit level view of the transport."""

    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    UNHEALTHY = "unhealthy"


class ClientSettings(BaseModel):
    """Everything needed to build one resilient asyncua client."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint_url: str
    application_uri: str = "urn:opcua-tutorial:client"
    application_name: str = "opcua-tutorial"
    session_name: str = "OPC UA Tutorial Client"
    session_timeout: float = Field(default=60.0, gt=0.0, description="seconds")
    request_timeout: float = Field(default=4.0, gt=0.0, description="seconds")
    watchdog_interval: float = Field(default=1.0, gt=0.0, description="seconds")
    max_concurrent_requests: int = Field(default=20, ge=1)
    username: str | None = None
    password: str | None = None
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    subscriptions: SubscriptionDefaults = Field(default_factory=SubscriptionDefaults)
    limits: LimitsOverrides = Field(default_factory=LimitsOverrides)

    @field_validator("endpoint_url")
    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        url = value.strip()
        if not url.startswith("opc.tcp://"):
            raise ValueError("endpoint_url must start with 'opc.tcp://'")
        return url

    @model_validator(mode="after")
    def _validate_credentials(self) -> ClientSettings:
        if self.username and not self.password:
            raise ConfigurationError("username set without password")
        return self

    def create_client(self) -> Client:
        """Build an asyncua client configured from these settings.

        The returned client is *not* connected: call ``await client.connect()``
        (or use it as an async context manager), or let
        :class:`~opcua_toolkit.resilience.ConnectionSupervisor` own the
        lifecycle as shown in Chapter 12.
        """
        client = Client(
            url=self.endpoint_url,
            timeout=self.request_timeout,
            watchdog_intervall=self.watchdog_interval,
            auto_reconnect=False,
        )
        client.application_uri = self.application_uri
        client.name = self.application_name
        client.description = self.session_name
        client.session_timeout = int(self.session_timeout * 1000)
        client.set_max_concurrent_requests(self.max_concurrent_requests)
        if self.username is not None and self.password is not None:
            client.set_user(self.username)
            client.set_password(self.password)
        self.security.apply(client)
        return client

    def describe(self) -> str:
        """Human readable summary used by the CLI, with no secrets."""
        return (
            f"endpoint={self.endpoint_url} app={self.application_uri} "
            f"session='{self.session_name}' timeout={self.session_timeout}s "
            f"security={self.security.describe()} "
            f"auth={'username+password' if self.username else 'anonymous'} "
            f"retry={self.retry.describe()}"
        )

    @classmethod
    def from_yaml(cls, source: str | Path) -> ClientSettings:
        """Load settings from a YAML file (used by Chapter 14 onwards)."""
        text = Path(source).read_text(encoding="utf-8")
        data: Any = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ConfigurationError(f"{source} must contain a YAML mapping")
        return cls.model_validate(data)

    def to_yaml(self) -> str:
        """Dump the settings as YAML, suitable for a config file."""
        payload = self.model_dump(mode="json")
        return yaml.safe_dump(payload, sort_keys=True)


__all__ = [
    "RECOMMENDED_POLICIES",
    "SUPPORTED_POLICIES",
    "ClientSettings",
    "ConnectionState",
    "LimitsOverrides",
    "RetryPolicy",
    "SecurityModeName",
    "SecurityPolicyName",
    "SecuritySettings",
    "SubscriptionDefaults",
]
