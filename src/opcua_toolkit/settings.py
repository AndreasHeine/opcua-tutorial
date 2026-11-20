"""Typed, validated client configuration.

One place that knows how to turn configuration into an asyncua ``Client``.
Every chapter that needs a connection gets it from
:meth:`ClientSettings.create_client`, so timeouts, security and retry policy
are never scattered across examples - and never silently different between the
tutorial code and the final project code.
"""

from __future__ import annotations

import dataclasses
import random
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from asyncua import Client, ua
from asyncua.crypto import security_policies
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from opcua_toolkit.errors import ConfigurationError, SecurityConfigurationError

SecurityPolicyName = Literal["None", "Basic256Sha256", "Aes128Sha256RsaOaep", "Aes256Sha256RsaPss"]
SecurityModeName = Literal["None", "Sign", "SignAndEncrypt"]


@dataclasses.dataclass(frozen=True, slots=True)
class DurationUnit:
    """How a duration may be written, and what a bare number means.

    A number is read in the unit the field documents (``publishing_interval: 500`` is
    500 ms because the field is a millisecond field). A string has to say what it
    means: ``"750ms"``, ``"2s"``, ``"2 seconds"``. A string that names no unit -
    ``"500"`` - is refused, because the one mistake every OPC UA config file makes is
    a *unit* mistake, and "500" is exactly as plausible in milliseconds as in seconds.

    The unit also stays in the field's pydantic metadata, which is how Chapter 14 prints
    ``publishing_interval = 500 ms`` instead of a bare number, and how a test can prove that
    every duration in the configuration declares its unit - instead of trusting a
    hand-written table that goes stale the moment somebody adds a field.
    """

    name: str
    milliseconds: float

    def parse(self, value: object) -> float | None:
        """Normalise one authored value to this field's unit.

        Raises:
            ValueError: Pydantic's validator contract: a ``ValueError`` from a before-validator
                becomes a field error with a message, which is what turns into one sentence in a
                config report. A ``TypeError`` here would escape the validation machinery instead.
        """
        if value is None:
            return None
        if isinstance(value, bool):
            # pydantic turns this ValueError into the field's error message, so it has to be a
            # ValueError with a sentence in it: a TypeError would escape the validation machinery.
            raise ValueError(  # noqa: TRY004
                f"{value!r} is not a duration: write a number of {self.name}"
            )
        if isinstance(value, int | float):
            return float(value)
        if not isinstance(value, str):
            raise ValueError(f"{value!r} is not a duration")  # noqa: TRY004
        text = value.strip().lower().replace(" ", "")
        if not text:
            raise ValueError("an empty string is not a duration")
        for suffix, unit in _UNIT_SUFFIXES.items():
            if not text.endswith(suffix):
                continue
            number = text[: -len(suffix)].strip()
            try:
                amount = float(number)
            except ValueError:
                raise ValueError(
                    f"{value!r}: '{suffix}' names a unit, but {number!r} is not a number"
                ) from None
            return amount * unit.milliseconds / self.milliseconds
        raise ValueError(
            f"{value!r} does not say which unit it is in: write {value}{self.name}, "
            "or name the unit you mean (ms, s)"
        )

    def format(self, value: float) -> str:
        """Render a value of this unit the way a report prints it: ``500 ms``, ``2.5 s``."""
        return f"{format_number(value)} {self.name}"


def format_number(value: float) -> str:
    """A number without a trailing ``.0``, so a report reads ``500`` and not ``500.0``.

    Every number this tutorial reports is one a reader compares with a number in a config file,
    and ``500.0`` next to ``500`` invites a second look that turns up nothing.
    """
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


_MILLISECOND = DurationUnit("ms", 1.0)
_SECOND = DurationUnit("s", 1000.0)


def format_milliseconds(value: float) -> str:
    """Render a millisecond-based duration the way a report prints it: ``500 ms``."""
    return _MILLISECOND.format(value)


def format_seconds(value: float) -> str:
    """Render a second-based duration the way a report prints it: ``90 s``."""
    return _SECOND.format(value)


#: Accepted string suffixes, longest-first so "milliseconds" is not read as "s".
_UNIT_SUFFIXES: dict[str, DurationUnit] = {
    "milliseconds": _MILLISECOND,
    "millisecond": _MILLISECOND,
    "msec": _MILLISECOND,
    "ms": _MILLISECOND,
    "seconds": _SECOND,
    "second": _SECOND,
    "secs": _SECOND,
    "sec": _SECOND,
    "s": _SECOND,
}

#: A float field whose numbers are milliseconds and whose strings must say ms or s.
Milliseconds = Annotated[float, _MILLISECOND, BeforeValidator(_MILLISECOND.parse)]
#: A float field whose numbers are seconds and whose strings must say s or ms.
Seconds = Annotated[float, _SECOND, BeforeValidator(_SECOND.parse)]
#: :data:`Milliseconds`, allowing ``null`` for "not set here" - what that means is decided by the
#: field that uses it, and each one says so in its own docstring.
MillisecondsOrNone = Annotated[float | None, _MILLISECOND, BeforeValidator(_MILLISECOND.parse)]
#: :data:`Seconds`, allowing ``null`` for "never do this at all".
SecondsOrNone = Annotated[float | None, _SECOND, BeforeValidator(_SECOND.parse)]


@dataclasses.dataclass(frozen=True, slots=True)
class ValueUnit:
    """What a number counts when it is not a duration.

    A subscription's ``lifetime_count`` is the most misread number in OPC UA configuration: it
    is not seconds and not milliseconds, it is a number of *publishing intervals*, so the same
    count is 5 seconds or 50 minutes depending on a field written somewhere else. Marking the
    unit on the field is what lets the configuration report say ``10000 publishing intervals``
    and let a test prove that no count in the configuration was left unlabeled.
    """

    name: str


_PUBLISHING_INTERVALS = ValueUnit("publishing intervals")
_NOTIFICATIONS_PER_ITEM = ValueUnit("notifications per monitored item")
_NOTIFICATIONS_PER_PUBLISH = ValueUnit("notifications per publish")
_ITEMS_PER_REQUEST = ValueUnit("items in one request")
_ATTEMPTS = ValueUnit("attempts")
_REQUESTS_IN_FLIGHT = ValueUnit("requests in flight")

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

    max_attempts: Annotated[int, _ATTEMPTS] = Field(
        default=0, ge=0, description="0 means retry forever"
    )
    initial_delay: Seconds = Field(default=0.5, gt=0.0)
    max_delay: Seconds = Field(default=30.0, gt=0.0)
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

    @model_validator(mode="after")
    def _require_policy_and_mode_to_agree(self) -> SecuritySettings:
        """A policy without a mode, or a mode without a policy, is not a channel.

        ``SecurityPolicyNone`` pairs with ``MessageSecurityMode.None_`` and nothing else:
        asking for Basic256Sha256 with mode None asks the server for an algorithm family
        with no defined use of it, and the usual result is an endpoint that is refused at
        activation, or an unsecured channel that a certificate file suggests is secured.
        Both directions are refused here because both are config-file typos, not choices.
        """
        if (self.policy == "None") != (self.mode == "None"):
            if self.policy == "None":
                raise SecurityConfigurationError(
                    f"mode={self.mode} with policy=None: sign or encrypt what? "
                    "Choose a security policy."
                )
            raise SecurityConfigurationError(
                f"policy={self.policy} with mode=None: a security policy needs mode Sign or "
                "SignAndEncrypt, otherwise it is not used at all"
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
    """The subscription's own timers and the defaults a monitored item inherits.

    Chapter 10 learned that a subscription is *negotiated*: the server answers with
    revised numbers. That only describes what arrives back - it says nothing about what
    this client asked for, and a client that never states its own request cannot compare
    the two. These are the numbers the toolkit sends in ``CreateSubscription``, now named
    in a config file instead of inherited from a client library's hardcoded defaults:

    Attributes:
        publishing_interval: Milliseconds between publishes.
        sampling_interval: Milliseconds between source reads. 0 means "as fast as the
            server can", which makes the queue the only thing limiting the data rate.
        queue_size: Notifications the server keeps per monitored item between publishes. A
            request of 0 or 1 asks for the server's default, which the specification puts at 1
            for a data item; a server may answer something else, and only ``revisedQueueSize``
            says what it used.
        lifetime_count: Publishing intervals this subscription may survive without a
            publish request before the server deletes it.
        max_keepalive_count: Publishing intervals of silence before the server sends an
            empty keepalive. ``None`` derives it from the session timeout, which is what
            the client library has always done - see :meth:`keepalive_count`.
        max_notifications_per_publish: Notification cap per publish response.
    """

    model_config = ConfigDict(frozen=True)

    publishing_interval: Milliseconds = Field(default=500.0, gt=0.0)
    sampling_interval: Milliseconds = Field(default=100.0, ge=0.0)
    queue_size: Annotated[int, _NOTIFICATIONS_PER_ITEM] = Field(default=10, ge=0)
    lifetime_count: Annotated[int, _PUBLISHING_INTERVALS] = Field(
        default=10_000,
        ge=1,
        description="publishing intervals a subscription survives without a publish request",
    )
    max_keepalive_count: Annotated[int | None, _PUBLISHING_INTERVALS] = Field(
        default=None, ge=0, description="publishing intervals of silence before a keepalive"
    )
    max_notifications_per_publish: Annotated[int, _NOTIFICATIONS_PER_PUBLISH] = Field(
        default=10_000, ge=1
    )

    @property
    def lifetime_seconds(self) -> float:
        """How long this subscription outlives a client that stopped publishing.

        The number is ``lifetime_count * publishing_interval``, and it is the subscription's
        own clock: a server deletes a subscription that has had no publish request to answer
        for that long, which is why the count is meaningless without the interval.
        """
        return self.lifetime_count * self.publishing_interval / 1000.0

    def keepalive_count(
        self, session_timeout_seconds: float, publishing_interval: float | None = None
    ) -> int:
        """The keepalive count this client will request.

        An explicit ``max_keepalive_count`` is sent exactly as written. Without one the
        count is derived from the session timeout the way asyncua's client has always
        derived it - ``int(session_timeout / publishing_interval * 0.75)`` - so a client
        that configures nothing gets what it used to get, and a client that configures
        something knows what it asked for. That derivation is the difference between a
        keepalive that arrives while the session is still alive and one that arrives just
        in time to report that the session is not.

        Args:
            session_timeout_seconds: The session this subscription lives in.
            publishing_interval: Override the interval the count counts. A
                :class:`~opcua_toolkit.SubscriptionManager` may publish on an interval
                different from the configured default.
        """
        if self.max_keepalive_count is not None:
            return self.max_keepalive_count
        interval = self.publishing_interval if publishing_interval is None else publishing_interval
        if interval <= 0.0:
            return 0
        return int((session_timeout_seconds * 1000.0 / interval) * 0.75)

    def keepalive_seconds(
        self, session_timeout_seconds: float, publishing_interval: float | None = None
    ) -> float:
        """How long this subscription may stay quiet before the server says anything."""
        interval = self.publishing_interval if publishing_interval is None else publishing_interval
        return self.keepalive_count(session_timeout_seconds, interval) * interval / 1000.0

    @property
    def changes_per_publish(self) -> float | None:
        """Changes one monitored item produces between two publishes, or ``None``.

        ``None`` when the sampling interval is 0: "as fast as the server can" has no
        predictable rate, which is exactly when a queue size matters most. Otherwise this
        is the number that says whether the queue can hold what the sampling produces -
        Chapter 10's queue-size lesson as an arithmetic check instead of an anecdote.
        """
        if self.sampling_interval <= 0.0:
            return None
        return self.publishing_interval / self.sampling_interval

    def summary(self, *, session_timeout_seconds: float) -> str:
        """One line naming every timer this client will ask the server for.

        The two counts are printed as what they are - numbers of publishing intervals - next to
        the duration they add up to at this publishing interval. A line that prints
        ``lifetime=10000`` leaves the multiplication to the reader, and that multiplication is the
        whole question being asked.
        """
        keepalive = self.keepalive_count(session_timeout_seconds)
        return (
            f"publishing={_MILLISECOND.format(self.publishing_interval)} "
            f"sampling={_MILLISECOND.format(self.sampling_interval)} "
            f"queue={self.queue_size} {_NOTIFICATIONS_PER_ITEM.name} "
            f"lifetime={self.lifetime_count} {_PUBLISHING_INTERVALS.name} "
            f"({_SECOND.format(self.lifetime_seconds)}) "
            f"keepalive={keepalive} {_PUBLISHING_INTERVALS.name} "
            f"({_SECOND.format(self.keepalive_seconds(session_timeout_seconds))}) "
            f"per_publish={self.max_notifications_per_publish} {_NOTIFICATIONS_PER_PUBLISH.name}"
        )


class LimitsOverrides(BaseModel):
    """Optional client-side caps, applied on top of what the server publishes."""

    model_config = ConfigDict(frozen=True)

    max_nodes_per_read: Annotated[int | None, _ITEMS_PER_REQUEST] = Field(default=None, gt=0)
    max_nodes_per_write: Annotated[int | None, _ITEMS_PER_REQUEST] = Field(default=None, gt=0)
    max_nodes_per_browse: Annotated[int | None, _ITEMS_PER_REQUEST] = Field(default=None, gt=0)
    max_nodes_per_method_call: Annotated[int | None, _ITEMS_PER_REQUEST] = Field(default=None, gt=0)
    max_monitored_items_per_call: Annotated[int | None, _ITEMS_PER_REQUEST] = Field(
        default=None, gt=0
    )

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
    session_timeout: Seconds = Field(default=60.0, gt=0.0)
    request_timeout: Seconds = Field(default=4.0, gt=0.0)
    watchdog_interval: Seconds = Field(default=1.0, gt=0.0)
    max_concurrent_requests: Annotated[int, _REQUESTS_IN_FLIGHT] = Field(default=20, ge=1)
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
    "DurationUnit",
    "LimitsOverrides",
    "Milliseconds",
    "MillisecondsOrNone",
    "RetryPolicy",
    "Seconds",
    "SecondsOrNone",
    "SecurityModeName",
    "SecurityPolicyName",
    "SecuritySettings",
    "SubscriptionDefaults",
    "ValueUnit",
    "format_milliseconds",
    "format_number",
    "format_seconds",
]
