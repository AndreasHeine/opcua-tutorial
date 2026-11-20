"""Configuration that refuses the mistakes a config file makes (Chapter 14).

Chapter 13 derived a client configuration from a server's answers. This chapter makes that
configuration *auditable*: typed values, declared units, unknown keys refused instead of
ignored, and a record - for every single value - of whether a human wrote it, an operator
overrode it in the environment, a server supplied it, or nobody thought about it and a
default answered.

The chapter's argument is the difference between three kinds of finding:

* a **refusal** is a contradiction between two numbers in the same configuration: it cannot
  do what it says it does. A subscription lifetime shorter than the reconnect delay that
  would have to save it, a keepalive window longer than the session that carries it, a
  request allowed to wait longer than the session that sends it, a tag in a namespace the
  server does not publish.
* a **review** is a number that works and does not do what you pictured: a queue that holds
  1 of the ~5 changes your own sampling produces between two publishes, a ``queue_size: 0``
  that hands the decision to the server, a retry policy that never retries.
* a **default in use** is neither of those. It is the honest statement that nobody chose this
  value. In a tutorial that is fine; on a plant it is an undocumented decision - so the report
  names it instead of hiding it.

Provenance is what makes the third kind visible. Every value knows where it came from -
``default``, ``derived`` (a server's own answer), ``file``, ``environment`` - and the
precedence is fixed: a human's written value always beats a discovered one, and a discovered
one always beats a default. An application can then say *which settings this deployment must
decide* and refuse to start while a default answers for them.

A subscription lives inside a session, so its timers are configured under the client that owns
that session (``client.subscriptions``). That is not nesting for style: the keepalive window,
the session timeout and the publishing interval are three numbers that only make sense next to
each other, and they are in one section together.
"""

from __future__ import annotations

import dataclasses
import difflib
import functools
import os
import types
from collections.abc import Iterable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Union, get_args, get_origin

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic.fields import FieldInfo

from opcua_toolkit.discovery import ServerProfile
from opcua_toolkit.errors import ConfigurationError, EndpointNotFoundError, format_node_id
from opcua_toolkit.limits import OperationLimits
from opcua_toolkit.nodeids import to_nodeid
from opcua_toolkit.settings import (
    ClientSettings,
    DurationUnit,
    MillisecondsOrNone,
    Seconds,
    SecondsOrNone,
    SubscriptionDefaults,
    ValueUnit,
    format_milliseconds,
    format_number,
    format_seconds,
)
from opcua_toolkit.subscriptions.model import SubscriptionRevision

#: Environment variables with this prefix are read as configuration. Only scalars:
#: ``OPCUA_TUTORIAL_CLIENT__SESSION_TIMEOUT``, ``OPCUA_TUTORIAL_CLIENT__SUBSCRIPTIONS__QUEUE_SIZE``.
ENV_PREFIX = "OPCUA_TUTORIAL_"

#: Nested setting paths in environment variable names: ``CLIENT__REQUEST_TIMEOUT``.
ENV_SEPARATOR = "__"

_REDACTED = "<kept out of this output>"

#: Client-side limit overrides that can contradict a limit the server publishes. Named here
#: so the review and :class:`~opcua_toolkit.settings.LimitsOverrides` cannot drift apart.
_LIMIT_FIELDS: tuple[str, ...] = (
    "max_nodes_per_read",
    "max_nodes_per_write",
    "max_nodes_per_browse",
    "max_nodes_per_method_call",
    "max_monitored_items_per_call",
)

#: Publishing intervals below this are faster than any server measured in this tutorial
#: publishes at, so a configuration that asks for one should expect a revision.
_PUBLISHING_REVIEW_MS = 10.0


class ConfigLayer(StrEnum):
    """Where one configured value came from.

    Ordered by authority: :attr:`ConfigLayer.ENVIRONMENT` beats :attr:`ConfigLayer.FILE`,
    which beats :attr:`ConfigLayer.DERIVED` (a server's own answer), which beats
    :attr:`ConfigLayer.DEFAULT` (nobody decided). A discovered value filling a gap is useful;
    a discovered value quietly replacing an operator's written one is the failure Chapter 13
    exists to prevent.
    """

    DEFAULT = "default"
    DERIVED = "derived"
    FILE = "file"
    ENVIRONMENT = "environment"


#: The layers that count as "this deployment decided it".
DECIDING_LAYERS: frozenset[ConfigLayer] = frozenset({ConfigLayer.FILE, ConfigLayer.ENVIRONMENT})


class FindingSeverity(StrEnum):
    """How much a review finding has to be acted on.

    Attributes:
        REFUSAL: Two configured values contradict each other, or a value is impossible on the
            server being asked. The toolkit refuses to run a configuration that cannot behave
            the way it claims to.
        REVIEW: Legal, and probably not what the author pictured. Reported, not refused -
            refusing everything that is merely unwise is how people end up switching the review
            off altogether.
    """

    REFUSAL = "refusal"
    REVIEW = "review"


def _is_secret(path: str) -> bool:
    """Whether a value must never appear in a report.

    Passwords are the only ones. A *path* to a private key file is operational information an
    operator needs in front of them, and this module never reads key contents while reporting
    where the key lives.
    """
    return path.endswith("password")


@dataclasses.dataclass(frozen=True, slots=True)
class ValueOrigin:
    """One value as it was authored, and who authored it.

    Attributes:
        path: Dotted setting path, e.g. ``client.subscriptions.queue_size``.
        layer: Which layer answered for it.
        source: The file, environment pattern or derivation that supplied it.
        authored: The value exactly as written, before any unit parsing - ``"2s"`` stays
            ``"2s"`` here even though the field stores 2000.0 milliseconds.
    """

    path: str
    layer: ConfigLayer
    source: str
    authored: Any = None

    @property
    def is_secret(self) -> bool:
        """True when this value must be redacted from any report."""
        return _is_secret(self.path)

    @property
    def is_decided(self) -> bool:
        """True when a human or an operator wrote this value."""
        return self.layer in DECIDING_LAYERS

    def describe(self) -> str:
        """One line naming the value, its authored form and its source."""
        text = _REDACTED if self.is_secret else _render(self.authored)
        return f"{self.path} = {text}  [{self.layer.value}: {self.source}]"

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "path": self.path,
            "layer": self.layer.value,
            "source": self.source,
            "authored": None if self.is_secret else _json_safe(self.authored),
        }


class ConfigProvenance:
    """Where each authored value of a resolved configuration came from.

    Only values that were authored - by a file, an environment variable or a discovery step -
    are recorded here. A setting with no entry is one nobody wrote, which is exactly the
    definition of a default, and :meth:`ResolvedConfig.resolved_values` labels those
    ``default``.
    """

    def __init__(self, origins: Iterable[ValueOrigin] = ()) -> None:
        self._origins: dict[str, ValueOrigin] = {origin.path: origin for origin in origins}

    def get(self, path: str) -> ValueOrigin | None:
        """The origin of one setting, or ``None`` when nobody authored it."""
        return self._origins.get(path)

    def authored(self) -> tuple[ValueOrigin, ...]:
        """Every authored origin, sorted by setting path."""
        return tuple(self._origins[key] for key in sorted(self._origins))

    def authored_in(self, *layers: ConfigLayer) -> tuple[ValueOrigin, ...]:
        """Authored origins that came from the given layers."""
        return tuple(origin for origin in self.authored() if origin.layer in layers)

    def paths(self) -> tuple[str, ...]:
        """Every authored setting path."""
        return tuple(sorted(self._origins))

    def describe(self) -> tuple[str, ...]:
        """One line per authored value: what it says, and who said it."""
        return tuple(origin.describe() for origin in self.authored())

    def __len__(self) -> int:
        """Number of authored values."""
        return len(self._origins)


@dataclasses.dataclass(frozen=True, slots=True)
class ConfigFinding:
    """One sentence a configuration has to read.

    Attributes:
        severity: Whether this finding stops the application or only asks to be read.
        path: The setting path the finding is about, so the sentence can be acted on.
        message: The finding, written as the arithmetic that produced it.
    """

    severity: FindingSeverity
    path: str
    message: str

    def describe(self) -> str:
        """One line of report output."""
        return f"{self.severity.value}: {self.path}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {"severity": self.severity.value, "path": self.path, "message": self.message}


@dataclasses.dataclass(frozen=True, slots=True)
class ResolvedValue:
    """A setting's effective value, its unit, and who decided it.

    Attributes:
        path: Dotted setting path, e.g. ``client.subscriptions.publishing_interval``.
        value: The value as the toolkit's model holds it.
        unit: The unit the field itself declared: ``ms`` and ``s`` for durations, ``publishing
            intervals`` for the subscription's counts, ``None`` for anything without a declared
            unit. It is read off the field's type, not from a hand-written table - which is why a
            new setting cannot be reported without one.
        layer: Which layer answered for this setting.
        source: The file, environment pattern or derivation that supplied it.
        authored: The value as written when it differs from the parsed one: ``"2s"`` in a
            millisecond field is reported as ``2000 ms (authored '2s')``.
    """

    path: str
    value: Any
    unit: str | None
    layer: ConfigLayer
    source: str
    authored: Any = None

    @property
    def is_secret(self) -> bool:
        """True when this value is redacted from reports."""
        return _is_secret(self.path)

    @property
    def is_default(self) -> bool:
        """True when nobody authored this value."""
        return self.layer is ConfigLayer.DEFAULT

    def shown(self) -> str:
        """The value as a human reads it: unit attached, secrets withheld."""
        if self.is_secret:
            return _REDACTED
        if self.unit is not None and isinstance(self.value, int | float):
            return f"{format_number(self.value)} {self.unit}"
        return _render(self.value)

    def describe(self) -> str:
        """One line: value, authored form if it differs, and where it came from."""
        suffix = ""
        if self.authored is not None and _is_scalar(self.authored) and self.authored != self.value:
            suffix = f" (authored {self.authored!r})"
        return f"{self.path} = {self.shown()}{suffix}  [{self.layer.value}: {self.source}]"

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "path": self.path,
            "value": None if self.is_secret else _json_safe(self.value),
            "unit": self.unit,
            "layer": self.layer.value,
            "source": self.source,
            "authored": None if self.is_secret else _json_safe(self.authored),
        }


@dataclasses.dataclass(frozen=True, slots=True)
class ConfigReview:
    """The outcome of asking a configuration whether it contradicts itself.

    Attributes:
        subscription_request: The subscription timers this configuration will ask a server for,
          in one line. Every timer finding below is measured against them.
        findings: Refusals first, then reviews. A reader who acts in order acts on what matters.
        defaults_in_use: Values nobody authored. Not a warning: a fact.
        derived_in_use: Values a server supplied and this configuration accepted.
    """

    subscription_request: str
    findings: tuple[ConfigFinding, ...] = ()
    defaults_in_use: tuple[ResolvedValue, ...] = ()
    derived_in_use: tuple[ResolvedValue, ...] = ()

    @property
    def refusals(self) -> tuple[ConfigFinding, ...]:
        """Findings that make this configuration impossible to run."""
        return tuple(f for f in self.findings if f.severity is FindingSeverity.REFUSAL)

    @property
    def notes(self) -> tuple[ConfigFinding, ...]:
        """Findings worth reading that do not stop the application."""
        return tuple(f for f in self.findings if f.severity is FindingSeverity.REVIEW)

    @property
    def is_startable(self) -> bool:
        """True when nothing in this configuration contradicts itself."""
        return not self.refusals

    def require_startable(self) -> None:
        """Refuse to continue when the review found a contradiction.

        Raises:
            ConfigurationError: One sentence per refusal, so a startup log says what has to be
                changed instead of failing later inside a transport or a subscription.
        """
        if not self.refusals:
            return
        details = "; ".join(f"{finding.path}: {finding.message}" for finding in self.refusals)
        raise ConfigurationError(f"configuration contradicts itself: {details}")

    def describe(self) -> tuple[str, ...]:
        """A report a human can act on, in the order the findings matter."""
        lines = [
            f"this client will ask for: {self.subscription_request}",
            f"refusals: {len(self.refusals)}  reviews: {len(self.notes)}",
        ]
        lines.extend(finding.describe() for finding in self.findings)
        if self.derived_in_use:
            lines.append(f"{len(self.derived_in_use)} value(s) came from the server:")
            lines.extend(f"  {value.describe()}" for value in self.derived_in_use)
        if self.defaults_in_use:
            lines.append(f"{len(self.defaults_in_use)} value(s) nobody decided:")
            lines.extend(f"  {value.describe()}" for value in self.defaults_in_use)
        return tuple(lines)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "subscription_request": self.subscription_request,
            "startable": self.is_startable,
            "refusals": [finding.as_dict() for finding in self.refusals],
            "reviews": [finding.as_dict() for finding in self.notes],
            "derived": [value.as_dict() for value in self.derived_in_use],
            "defaults": [value.as_dict() for value in self.defaults_in_use],
        }


class TagSettings(BaseModel):
    """One tag this application monitors, and how it finds it.

    Attributes:
        name: The label used by dispatch keys, buffers, logs and every report.
        node_id: Canonical NodeId text (``ns=2;s=Temperature``). Use it when the address space
            is known and stable.
        path: A browse path (``ProductionLine/Line1/Temperature``). Use it when the namespace
            index is not known: Chapter 13 resolves it against the running server.
        sampling_interval: Override for this tag only. ``None`` inherits the subscription's.
        queue_size: Override for this tag only. ``None`` inherits the subscription's.
        monitored: ``False`` keeps the tag in the configuration, and in every report, without
            creating a monitored item for it. A disabled tag is a decision somebody can read; a
            deleted line is not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    node_id: str | None = None
    path: str | None = None
    sampling_interval: MillisecondsOrNone = Field(default=None, ge=0.0)
    queue_size: int | None = Field(default=None, ge=0)
    monitored: bool = True

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("a tag needs a name")
        return text

    @field_validator("node_id")
    @classmethod
    def _canonicalise_node_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return format_node_id(to_nodeid(value))

    @model_validator(mode="after")
    def _exactly_one_locator(self) -> TagSettings:
        if self.node_id is not None and self.path is not None:
            raise ValueError(
                f"tag {self.name!r} has both node_id and path: choose one. A node_id says "
                "'this node, in this namespace'; a path says 'find the node'. Answering both "
                "questions means one of them is decoration"
            )
        if self.node_id is None and self.path is None:
            raise ValueError(f"tag {self.name!r} needs node_id or path")
        return self


class SupervisionSettings(BaseModel):
    """How Chapter 12's supervisor watches this link and these subscriptions.

    Attributes:
        poll_interval: Seconds between health probes while the server answers.
        silent_probe_limit: Seconds a subscription may stay quiet before the supervisor asks the
            server whether it still has it. ``None`` never asks. Silence alone is not damage - a
            plant at steady state changes nothing - so it triggers a question and never a rebuild.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    poll_interval: Seconds = Field(default=5.0, gt=0.0)
    silent_probe_limit: SecondsOrNone = Field(default=60.0, gt=0.0)


class ApplicationConfig(BaseModel):
    """One application's worth of OPC UA configuration.

    Attributes:
        client: The connection and the subscriptions inside it: endpoint, identity, security,
            timeouts, retry policy, client-side limit overrides, subscription timers.
        supervision: How the link is watched, and how long silence waits before it is questioned.
        tags: What this application monitors. An empty list is legal and is reported as such.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    client: ClientSettings
    supervision: SupervisionSettings = Field(default_factory=SupervisionSettings)
    tags: tuple[TagSettings, ...] = ()

    @model_validator(mode="after")
    def _tags_are_distinct(self) -> ApplicationConfig:
        names: set[str] = set()
        nodes: dict[str, str] = {}
        for tag in self.tags:
            if tag.name in names:
                raise ConfigurationError(
                    f"tag name {tag.name!r} appears twice: dispatch keys, buffers and logs all use "
                    "it, so two tags with one name cannot be told apart"
                )
            names.add(tag.name)
            if tag.node_id is None:
                continue
            if tag.node_id in nodes:
                raise ConfigurationError(
                    f"{tag.node_id} is configured twice (tags {nodes[tag.node_id]!r} and "
                    f"{tag.name!r}): that is two monitored items on one node, and two copies of "
                    "every notification it produces"
                )
            nodes[tag.node_id] = tag.name
        return self

    @property
    def subscriptions(self) -> SubscriptionDefaults:
        """The subscription timers, which live with the client that owns the session."""
        return self.client.subscriptions

    def subscription_request(self) -> str:
        """One line naming every subscription timer this configuration will ask for."""
        return self.subscriptions.summary(session_timeout_seconds=self.client.session_timeout)

    def review(self) -> ConfigReview:
        """Ask this configuration whether its own numbers contradict each other.

        Returns:
            A :class:`ConfigReview`. Refusals here are contradictions between two configured
            values; reviews are numbers that work and probably do not mean what the author
            pictured. Findings that need provenance - a default still in use, a password written
            in a file - and findings that need a server belong to :class:`ResolvedConfig`.
        """
        findings = [
            *_timer_findings(self),
            *_queue_findings(self),
            *_supervision_findings(self),
            *_retry_findings(self),
        ]
        return ConfigReview(
            subscription_request=self.subscription_request(),
            findings=_ordered_findings(findings),
        )


def _timer_findings(config: ApplicationConfig) -> tuple[ConfigFinding, ...]:
    """The subscription timers, measured against each other and against the session.

    A subscription has three clocks a config file writes separately - publishing interval,
    lifetime count, keepalive count - and one clock it does not own at all: the session. Each
    finding below is the arithmetic that shows one of them outrunning another.
    """
    client = config.client
    subscriptions = config.subscriptions
    keepalive = subscriptions.keepalive_seconds(client.session_timeout)
    lifetime = subscriptions.lifetime_seconds
    session = format_seconds(client.session_timeout)
    findings: list[ConfigFinding] = []

    if keepalive > client.session_timeout:
        findings.append(
            ConfigFinding(
                FindingSeverity.REFUSAL,
                "client.subscriptions.max_keepalive_count",
                f"the server is asked to speak after {format_seconds(keepalive)} of silence, but "
                f"the session carrying this subscription times out after {session}: the keepalive "
                "would arrive after the subscription had gone with the session",
            )
        )
    if lifetime < client.retry.max_delay:
        findings.append(
            ConfigFinding(
                FindingSeverity.REFUSAL,
                "client.subscriptions.lifetime_count",
                f"this subscription may survive {format_seconds(lifetime)} without a publish "
                f"request, while the reconnect policy waits up to "
                f"{format_seconds(client.retry.max_delay)} before asking again: the server deletes "
                "the subscription before the client comes back",
            )
        )
    if client.request_timeout >= client.session_timeout:
        findings.append(
            ConfigFinding(
                FindingSeverity.REFUSAL,
                "client.request_timeout",
                f"a request may wait {format_seconds(client.request_timeout)} for an answer, which "
                f"is at least as long as the {session} session that sends it",
            )
        )
    if client.watchdog_interval >= client.session_timeout:
        findings.append(
            ConfigFinding(
                FindingSeverity.REVIEW,
                "client.watchdog_interval",
                f"the watchdog wakes every {format_seconds(client.watchdog_interval)} and the "
                f"session times out after {session}: a dead channel can be noticed only after the "
                "session is already gone",
            )
        )
    if lifetime > client.session_timeout:
        findings.append(
            ConfigFinding(
                FindingSeverity.REVIEW,
                "client.subscriptions.lifetime_count",
                f"this subscription outlives a stopped publish request by "
                f"{format_seconds(lifetime)}, but its session times out after {session}: on a "
                "session loss the lifetime buys nothing, because the subscription goes with it",
            )
        )
    if subscriptions.publishing_interval < _PUBLISHING_REVIEW_MS:
        findings.append(
            ConfigFinding(
                FindingSeverity.REVIEW,
                "client.subscriptions.publishing_interval",
                f"{format_milliseconds(subscriptions.publishing_interval)} between publishes is "
                "faster than any server measured in this tutorial publishes at; expect the server "
                "to revise it, and read what it answered instead of what was asked",
            )
        )
    return tuple(findings)


def _queue_findings(config: ApplicationConfig) -> tuple[ConfigFinding, ...]:
    """Whether the queues can hold what the configured sampling produces.

    A monitored item reports *changes*, and the server keeps only ``queue_size`` of them between
    two publishes. Sampling and publishing are configured separately, so the mismatch is invisible
    until data is missing. These findings do the arithmetic up front.
    """
    subscriptions = config.subscriptions
    changes = subscriptions.changes_per_publish
    sampling = format_milliseconds(subscriptions.sampling_interval)
    publishing = format_milliseconds(subscriptions.publishing_interval)
    findings: list[ConfigFinding] = []

    if subscriptions.queue_size == 0:
        findings.append(
            ConfigFinding(
                FindingSeverity.REVIEW,
                "client.subscriptions.queue_size",
                "queue_size=0 asks for the server's default queue: Part 4 gives a data monitored "
                "item's request of 0 and 1 the same meaning and says that default is 1, so this "
                "client is asking for a queue of one - and the number the server really uses is "
                "only readable from its answer",
            )
        )
    elif changes is not None and changes > subscriptions.queue_size:
        lost = changes - subscriptions.queue_size
        findings.append(
            ConfigFinding(
                FindingSeverity.REVIEW,
                "client.subscriptions.queue_size",
                f"sampling every {sampling} and publishing every {publishing} produces about "
                f"{changes:g} changes per publish, and the queue holds "
                f"{subscriptions.queue_size}: about {lost:g} of them are discarded between two "
                "publishes whenever the value keeps changing",
            )
        )
    elif changes is not None and changes < 1.0:
        findings.append(
            ConfigFinding(
                FindingSeverity.REVIEW,
                "client.subscriptions.sampling_interval",
                f"the source is sampled every {sampling} while publishes leave every {publishing}"
                ": most publishes carry nothing new, which is harmless but is not the data rate "
                "this pairing suggests",
            )
        )

    findings.extend(_publish_capacity_findings(config))
    return tuple(findings)


def _publish_capacity_findings(config: ApplicationConfig) -> tuple[ConfigFinding, ...]:
    """Whether one publish response can carry what all the monitored items can produce."""
    subscriptions = config.subscriptions
    monitored = [tag for tag in config.tags if tag.monitored]
    if not monitored:
        return ()

    capacity = sum(
        subscriptions.queue_size if tag.queue_size is None else tag.queue_size for tag in monitored
    )
    if capacity == 0:
        return ()
    if capacity > subscriptions.max_notifications_per_publish:
        return (
            ConfigFinding(
                FindingSeverity.REVIEW,
                "client.subscriptions.max_notifications_per_publish",
                f"{len(monitored)} monitored item(s) can produce {capacity} notifications between "
                f"two publishes while one publish may carry at most "
                f"{subscriptions.max_notifications_per_publish}: the rest wait for a later publish",
            ),
        )
    return ()


def _supervision_findings(config: ApplicationConfig) -> tuple[ConfigFinding, ...]:
    """The supervision window, measured against the server's own keepalive.

    Chapter 12's rule is that silence is a reason to ask, not a reason to rebuild. This is the
    arithmetic of *when the asking happens*: the supervisor's window, the server's keepalive, and
    the poll that carries both.
    """
    supervision = config.supervision
    keepalive = config.subscriptions.keepalive_seconds(config.client.session_timeout)
    findings: list[ConfigFinding] = []

    if supervision.silent_probe_limit is None:
        findings.append(
            ConfigFinding(
                FindingSeverity.REVIEW,
                "supervision.silent_probe_limit",
                "silence is never questioned: the supervisor will not ask this server whether it "
                "still has a subscription, so a server-side delete shows up only as missing values",
            )
        )
        return tuple(findings)

    limit = supervision.silent_probe_limit
    window = format_seconds(limit)
    server_silence = format_seconds(keepalive)
    poll = format_seconds(supervision.poll_interval)
    if limit < keepalive:
        findings.append(
            ConfigFinding(
                FindingSeverity.REVIEW,
                "supervision.silent_probe_limit",
                f"the supervisor asks about silence after {window}; this server's keepalive "
                f"would speak after {server_silence}. The client asks first, which costs one "
                "cheap request per window and does not wait for the server's permission",
            )
        )
    elif limit == keepalive:
        findings.append(
            ConfigFinding(
                FindingSeverity.REVIEW,
                "supervision.silent_probe_limit",
                f"the supervisor's window of {window} is exactly this server's keepalive of "
                f"{server_silence}: the question and the keepalive land at the same moment, "
                "which is the cheapest arrangement available - and it holds only while the "
                "publishing interval does not change",
            )
        )
    else:
        gap = format_seconds(limit - keepalive)
        findings.append(
            ConfigFinding(
                FindingSeverity.REVIEW,
                "supervision.silent_probe_limit",
                f"this server would report the subscription every {server_silence} of silence, "
                f"and the supervisor only asks after {window}: {gap} of that window is spent "
                "waiting for the server to speak. When the server stops speaking while the "
                "socket stays open, that gap is how long nothing is asked",
            )
        )
    if supervision.poll_interval > limit:
        findings.append(
            ConfigFinding(
                FindingSeverity.REVIEW,
                "supervision.poll_interval",
                f"the supervisor polls every {poll} while it watches a silence window of "
                f"{window}: the window is exceeded by a whole poll before anything looks at it",
            )
        )
    return tuple(findings)


def _retry_findings(config: ApplicationConfig) -> tuple[ConfigFinding, ...]:
    """What the retry policy will actually do, in its own numbers."""
    retry = config.client.retry
    if retry.max_attempts == 1:
        return (
            ConfigFinding(
                FindingSeverity.REVIEW,
                "client.retry.max_attempts",
                "max_attempts=1 means the first failure is also the last attempt: nothing is "
                "retried. That is a decision to fail fast, not a policy to keep a link up",
            ),
        )
    return ()


def _ordered_findings(findings: Iterable[ConfigFinding]) -> tuple[ConfigFinding, ...]:
    """Refusals first, then reviews, each group in the order it was produced."""
    refused = tuple(f for f in findings if f.severity is FindingSeverity.REFUSAL)
    noted = tuple(f for f in findings if f.severity is FindingSeverity.REVIEW)
    return refused + noted


@functools.lru_cache(maxsize=1)
def config_fields() -> dict[str, FieldInfo]:
    """Every dotted setting path in :class:`ApplicationConfig`, including nested models.

    Built from the models themselves rather than from a table: a setting added to
    ``SubscriptionDefaults`` appears in the review, in the unknown-key check and in the
    resolved-value report with no further work. That is the difference between a configuration
    layer and a list of strings somebody has to remember to update.
    """
    return _collect_fields(ApplicationConfig)


@functools.lru_cache(maxsize=1)
def leaf_fields() -> dict[str, FieldInfo]:
    """Setting paths that hold a value rather than another section."""
    return {
        path: info for path, info in config_fields().items() if not _nested_models(info.annotation)
    }


@functools.lru_cache(maxsize=1)
def section_paths() -> frozenset[str]:
    """Setting paths that hold another section, e.g. ``client`` or ``client.security``.

    A section written as a scalar - ``client: opc.tcp://localhost:4840`` - is a mistake a reader
    should be told about as a section, not as an unknown key.
    """
    return frozenset(path for path in config_fields() if path not in leaf_fields())


def _collect_fields(model: type[BaseModel], prefix: str = "") -> dict[str, FieldInfo]:
    fields: dict[str, FieldInfo] = {}
    for name, info in model.model_fields.items():
        path = f"{prefix}{name}"
        fields[path] = info
        for nested in _nested_models(info.annotation):
            fields.update(_collect_fields(nested, f"{path}."))
    return fields


_UNION_ORIGINS: tuple[Any, ...] = (types.UnionType, Union)


def _nested_models(annotation: Any) -> list[type[BaseModel]]:
    """The models reachable through one field's annotation, without descending into lists.

    ``SecuritySettings | None`` still describes one settings section, so it is descended into.
    ``tuple[TagSettings, ...]`` describes a *value* that happens to be a list of records, and a
    list of tags is authored as one value: descending into it would turn ``tags`` into an unknown
    setting and invent paths like ``tags.name`` that no config file can write.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    if get_origin(annotation) in _UNION_ORIGINS:
        found: list[type[BaseModel]] = []
        for argument in get_args(annotation):
            found.extend(_nested_models(argument))
        return found
    return []


def _unit_of(info: FieldInfo) -> str | None:
    """The unit a field declared, read from the field's own type.

    :data:`~opcua_toolkit.settings.Milliseconds` and :data:`~opcua_toolkit.settings.Seconds` carry
    a :class:`~opcua_toolkit.settings.DurationUnit` in their pydantic metadata; the subscription's
    counts carry a :class:`~opcua_toolkit.settings.ValueUnit`. Reading the unit from the field is
    what makes ``500 ms`` and ``10000 publishing intervals`` printable, and what makes it impossible
    for a configured number to be reported without saying what it counts.
    """
    for metadata in getattr(info, "metadata", ()):
        if isinstance(metadata, DurationUnit | ValueUnit):
            return metadata.name
    return None


@functools.lru_cache(maxsize=1)
def list_element_fields() -> dict[str, dict[str, FieldInfo]]:
    """Setting names inside a value that is a list of records, keyed by the list's own path.

    ``tags`` is a leaf: a config file writes the whole list in one place. But a typo *inside* one
    entry is still a typo against a model, and pydantic reports it as ``tags.0.node_is``. Knowing
    the element model's fields is what turns that into "did you mean ``tags.node_id``?".
    """
    elements: dict[str, dict[str, FieldInfo]] = {}
    for path, info in leaf_fields().items():
        for model in _container_models(info.annotation):
            elements[path] = model.model_fields
    return elements


def _container_models(annotation: Any) -> list[type[BaseModel]]:
    """The models inside a list-valued field, e.g. ``tuple[TagSettings, ...]``."""
    if get_origin(annotation) not in (list, tuple, set, frozenset):
        return []
    return [
        argument
        for argument in get_args(annotation)
        if isinstance(argument, type) and issubclass(argument, BaseModel)
    ]


def _direct_children(section: str) -> tuple[str, ...]:
    """Setting names that live directly under one section, or inside one entry of a list value."""
    if section in list_element_fields():
        return tuple(sorted(list_element_fields()[section]))
    prefix = f"{section}." if section else ""
    children = {
        key[len(prefix) :]
        for key in leaf_fields()
        if key.startswith(prefix) and "." not in key[len(prefix) :]
    }
    return tuple(sorted(children))


def _suggestion(name: str, parent: str) -> str | None:
    """The closest real setting name to a misspelled one, if there is one.

    Numeric path parts - the index of a tag inside a list - are dropped, because a typo inside a
    list entry is a typo against that entry's model, and ``tags.0.node_is`` should be suggested
    as ``tags.node_id``.
    """
    parts = [part for part in parent.split(".") if not part.isdigit()]
    section = ".".join(parts)
    matches = difflib.get_close_matches(name, _direct_children(section), n=1, cutoff=0.4)
    if not matches:
        return None
    return f"{section}.{matches[0]}" if section else matches[0]


def _read_yaml(source: Path) -> Mapping[str, Any]:
    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot read {source}: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ConfigurationError(f"{source} must contain a mapping of settings")
    return document


def _flatten(mapping: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Turn nested settings into dotted leaf paths.

    Only mappings are descended into: lists are leaf values, because two sources of a tag list
    have no sane merge - the later layer replaces the earlier one wholesale, and a configuration
    that replaces a tag list is a decision worth seeing in the report.
    """
    leaves: dict[str, Any] = {}
    for key, value in mapping.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping):
            leaves.update(_flatten(value, f"{path}."))
        else:
            leaves[path] = value
    return leaves


def _unflatten(leaves: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild nested settings from dotted leaf paths."""
    nested: dict[str, Any] = {}
    for path, value in leaves.items():
        parts = path.split(".")
        current: dict[str, Any] = nested
        for index, part in enumerate(parts):
            if index == len(parts) - 1:
                current[part] = value
                continue
            following = current.get(part)
            if not isinstance(following, dict):
                following = {}
                current[part] = following
            current = following
    return nested


def _environment_leaves(environ: Mapping[str, str]) -> dict[str, Any]:
    """Read ``OPCUA_TUTORIAL_*`` variables as setting leaves.

    Values are parsed as YAML scalars so that an environment variable behaves like the same line
    in the file: ``90`` is the number 90, ``true`` is a boolean, ``2s`` is a duration string the
    field has to accept. Lists cannot be set this way, which is stated rather than worked around:
    an environment variable holding a tag list would be a second language invented for one use.
    """
    leaves: dict[str, Any] = {}
    for name, raw in environ.items():
        if not name.startswith(ENV_PREFIX) or name == ENV_PREFIX:
            continue
        remainder = name[len(ENV_PREFIX) :]
        dotted = remainder.lower().replace(ENV_SEPARATOR, ".")
        if not remainder or dotted.startswith(".") or dotted.endswith(".") or ".." in dotted:
            raise ConfigurationError(
                f"{name}: expected {ENV_PREFIX}SECTION{ENV_SEPARATOR}SETTING, e.g. "
                f"{ENV_PREFIX}CLIENT{ENV_SEPARATOR}SESSION_TIMEOUT"
            )
        leaves[dotted] = _scalar(name, raw)
    return leaves


def _scalar(name: str, raw: str) -> Any:
    """Parse one environment value as the YAML scalar it looks like.

    A string value stays a string, which is what makes ``2s`` work as a duration. A value that
    YAML reads as a list or a mapping is refused rather than quietly stringified: lists and
    sections belong in the file.
    """
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw
    if isinstance(parsed, Mapping | list | set | tuple | frozenset):
        raise ConfigurationError(
            f"{name}: an environment value must be one scalar; lists and sections belong in the "
            "configuration file"
        )
    if parsed is None and raw.strip() not in {"", "~", "null", "Null", "NULL"}:
        return raw
    return parsed


def _validation_message(exc: ValidationError) -> str:
    """Turn pydantic's error list into sentences a config author can fix.

    An unknown key is the case that deserves help: the message names the section it was found in
    and the closest setting that really exists, because "Extra inputs are not permitted" is a
    message that leaves the reader hunting through a file.
    """
    parts: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(item) for item in error["loc"])
        message = str(error["msg"])
        if error["type"] == "extra_forbidden":
            parent, _, last = loc.rpartition(".")
            hint = _suggestion(last, parent)
            message = f"unknown setting{' - did you mean ' + repr(hint) if hint else ''}"
        elif error["type"] == "missing":
            message = "required, and nothing supplies it"
        elif error["type"] == "value_error":
            message = message.removeprefix("Value error, ")
        parts.append(f"{loc or '<root>'}: {message}")
    return "configuration is invalid: " + "; ".join(parts)


def _reject_unknown_leaves(leaves: Mapping[str, Any], origins: Mapping[str, ValueOrigin]) -> None:
    """Refuse settings that no field defines, naming the nearest real setting.

    This is the refusal a config file needs most: a key nobody reads is a setting that was
    silently not applied, and the symptom shows up months later as a plant that appears to ignore
    its own publishing interval.
    """
    allowed = leaf_fields()
    sections = section_paths()
    problems: list[str] = []
    for path in sorted(leaves):
        if path in allowed:
            continue
        origin = origins[path]
        detail = f"{path} ({origin.layer.value}: {origin.source})"
        if path in sections:
            problems.append(
                f"{detail} is a section, not a value: write the settings below it "
                f"({', '.join(_direct_children(path))})"
            )
            continue
        parent, _, last = path.rpartition(".")
        hint = _suggestion(last, parent)
        if hint:
            detail += f" - did you mean {hint!r}?"
        problems.append(detail)
    if problems:
        raise ConfigurationError("unknown setting(s): " + "; ".join(problems))


def load_config(
    source: str | Path | Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    derived: Mapping[str, Any] | None = None,
    derived_source: str = "the server's own answers",
) -> ResolvedConfig:
    """Resolve one configuration from a file, the environment and what a server said.

    Args:
        source: Path to a YAML file, an already parsed mapping, or ``None`` for a configuration
            made only of defaults, environment variables and ``derived``.
        environ: Environment mapping. Defaults to :data:`os.environ`, reading only
            ``OPCUA_TUTORIAL_*``.
        derived: Settings taken from a running server (see :func:`discovery_settings`). They fill
            gaps only: a value somebody wrote is never replaced by a discovered one.
        derived_source: Name of the discovery step that produced them, used verbatim in the report
            so a log says where a number came from.

    Returns:
        A :class:`ResolvedConfig`: the validated configuration plus the provenance of every
        authored value.

    Raises:
        ConfigurationError: A key no field defines, a section written as a value, a value that
            cannot be parsed, a contradiction the settings model refuses, or a malformed
            environment variable name. One sentence each, before anything connects.
    """
    layers: list[tuple[ConfigLayer, str, dict[str, Any]]] = []
    if derived:
        layers.append((ConfigLayer.DERIVED, derived_source, _flatten(derived)))
    if isinstance(source, str | Path):
        layers.append((ConfigLayer.FILE, str(source), _flatten(_read_yaml(Path(source)))))
    elif isinstance(source, Mapping):
        layers.append((ConfigLayer.FILE, "given mapping", _flatten(source)))
    environment = os.environ if environ is None else environ
    env_leaves = _environment_leaves(environment)
    if env_leaves:
        layers.append((ConfigLayer.ENVIRONMENT, f"{ENV_PREFIX}* variables", env_leaves))

    leaves: dict[str, Any] = {}
    origins: dict[str, ValueOrigin] = {}
    for layer, name, values in layers:
        for path, value in values.items():
            leaves[path] = value
            origins[path] = ValueOrigin(path=path, layer=layer, source=name, authored=value)

    _reject_unknown_leaves(leaves, origins)
    try:
        config = ApplicationConfig.model_validate(_unflatten(leaves))
    except ValidationError as exc:
        raise ConfigurationError(_validation_message(exc)) from exc
    return ResolvedConfig(
        config=config,
        provenance=ConfigProvenance(origins.values()),
        sources=tuple(name for _, name, _ in layers),
    )


def discovery_settings(profile: ServerProfile) -> dict[str, Any]:
    """The settings a server is authoritative about, as a ``derived`` layer.

    Chapter 13's discovery answered these questions already. Feeding them back as a derived layer
    - instead of pasting them into a file - keeps the distinction that matters: the file says what
    the application decided, the derived layer says what the server said, and the report shows
    which is which.

    Only what a server can legitimately answer for a *client* is derived: the endpoint URL it
    publishes, and the fastest sample rate it publishes. Its application URI, its namespace
    indices and its own session timeouts are facts about the server, not decisions for this client
    - :class:`ResolvedConfig` reports a contradiction between those and the configuration rather
    than silently adopting them.
    """
    settings: dict[str, Any] = {"client": {"endpoint_url": profile.endpoint_url}}
    limits = profile.limits
    if limits is not None and limits.min_supported_sample_rate is not None:
        client = settings["client"]
        client["subscriptions"] = {"sampling_interval": limits.min_supported_sample_rate}
    return settings


@dataclasses.dataclass(frozen=True, slots=True)
class ResolvedConfig:
    """A configuration plus the record of who decided each value.

    Attributes:
        config: The validated configuration.
        provenance: Where each authored value came from.
        sources: The layers that were read, in precedence order.
    """

    config: ApplicationConfig
    provenance: ConfigProvenance
    sources: tuple[str, ...] = ()

    def resolved_values(self) -> tuple[ResolvedValue, ...]:
        """Every setting, with its effective value, its declared unit and its origin.

        Settings nobody authored are reported as defaults with the toolkit's own default as their
        source - the part of a production configuration review that people find uncomfortable, and
        the reason this chapter writes it down.
        """
        values: list[ResolvedValue] = []
        for path, info in leaf_fields().items():
            origin = self.provenance.get(path)
            values.append(
                ResolvedValue(
                    path=path,
                    value=_read_value(self.config, path),
                    unit=_unit_of(info),
                    layer=origin.layer if origin else ConfigLayer.DEFAULT,
                    source=origin.source if origin else "the toolkit's default",
                    authored=origin.authored if origin else None,
                )
            )
        return tuple(values)

    def review(self) -> ConfigReview:
        """Contradictions the model can see, plus the findings only provenance can produce.

        Returns:
            :meth:`ApplicationConfig.review` extended with the derived values this configuration
            accepted, the values nobody decided, and any password authored in a file - a secret in
            a file readable by whoever can read the deployment is not a contradiction, and it is
            not a thing a settings model can see.
        """
        review = self.config.review()
        findings = list(review.findings)
        findings.extend(self._provenance_findings())
        values = self.resolved_values()
        return dataclasses.replace(
            review,
            findings=_ordered_findings(findings),
            derived_in_use=tuple(value for value in values if value.layer is ConfigLayer.DERIVED),
            defaults_in_use=tuple(value for value in values if value.layer is ConfigLayer.DEFAULT),
        )

    def check_server(self, profile: ServerProfile) -> ConfigReview:
        """Ask a running server whether this configuration is possible on it.

        Returns:
            The configuration's own review plus the contradictions only a server can produce: a tag
            in a namespace this server does not publish, a security profile this server does not
            offer, a client-side limit override more generous than the limit this server publishes,
            a sampling interval below the floor this server publishes.

        Note:
            A limit the server did not publish is compared against nothing. The toolkit's assumed
            default is a guess, and refusing an override because it disagrees with a guess would be
            the mistake this tutorial keeps warning about.
        """
        review = self.review()
        findings = list(review.findings)
        findings.extend(self._server_findings(profile))
        return dataclasses.replace(review, findings=_ordered_findings(findings))

    def check_subscription(self, revision: SubscriptionRevision) -> ConfigReview:
        """Compare what this configuration asked for with what the server answered.

        :meth:`review` compares a configuration with itself, and :meth:`check_server` compares it
        with what a server publishes. This is the comparison with what a server *answered*: the
        :class:`~opcua_toolkit.SubscriptionRevision` a live ``CreateSubscription`` produced, which
        carries both the request and the answer.

        A server moving a number is not a mistake - it is entitled to. It becomes a finding when the
        answered number breaks a promise this configuration makes somewhere else: a lifetime the
        server shortened to under what the reconnect policy needs, a keepalive that now arrives
        later than the supervision window waiting for it.

        Args:
            revision: Requested and revised subscription timers, as reported by
                :meth:`~opcua_toolkit.SubscriptionManager.confirm_subscription`.

        Returns:
            The configuration's own review plus one finding per timer this server changed.
        """
        review = self.review()
        findings = list(review.findings)
        findings.extend(self._revision_findings(revision))
        return dataclasses.replace(review, findings=_ordered_findings(findings))

    def _revision_findings(self, revision: SubscriptionRevision) -> tuple[ConfigFinding, ...]:
        findings: list[ConfigFinding] = []

        if (
            revision.revised_publishing_interval is not None
            and revision.revised_publishing_interval != revision.requested_publishing_interval
        ):
            asked = format_milliseconds(revision.requested_publishing_interval)
            given = format_milliseconds(revision.effective_publishing_interval)
            findings.append(
                ConfigFinding(
                    FindingSeverity.REVIEW,
                    "client.subscriptions.publishing_interval",
                    f"this client asked for {asked} and this server publishes every {given}. Every "
                    "count in this configuration counts the server's interval, not the one that "
                    "was written down",
                )
            )

        if (
            revision.revised_lifetime_count is not None
            and revision.revised_lifetime_count != revision.requested_lifetime_count
        ):
            findings.append(self._lifetime_revision_finding(revision))

        if (
            revision.revised_max_keepalive_count is not None
            and revision.revised_max_keepalive_count != revision.requested_max_keepalive_count
        ):
            findings.append(self._keepalive_revision_finding(revision))

        return tuple(findings)

    def _lifetime_revision_finding(self, revision: SubscriptionRevision) -> ConfigFinding:
        """What the server's own lifetime cap means for this deployment's reconnect promise."""
        asked = (
            f"{revision.requested_lifetime_count} publishing intervals "
            f"({format_seconds(revision.requested_lifetime_seconds)})"
        )
        answered = revision.revised_lifetime_seconds or 0.0
        given = f"{revision.revised_lifetime_count} ({format_seconds(answered)})"
        delay = self.config.client.retry.max_delay
        if answered < delay:
            return ConfigFinding(
                FindingSeverity.REFUSAL,
                "client.subscriptions.lifetime_count",
                f"this client asked for {asked} and this server answered {given}, which is less "
                f"than the {format_seconds(delay)} the reconnect policy may wait before publishing "
                "again: the server deletes the subscription before the client comes back",
            )
        return ConfigFinding(
            FindingSeverity.REVIEW,
            "client.subscriptions.lifetime_count",
            f"this client asked for {asked} and this server answered {given}. The answer is the "
            "lifetime that runs, and it is the number a reconnect has to fit inside",
        )

    def _keepalive_revision_finding(self, revision: SubscriptionRevision) -> ConfigFinding:
        """The server's keepalive, measured against the supervision window that waits for it."""
        asked = (
            f"{revision.requested_max_keepalive_count} publishing intervals "
            f"({format_seconds(revision.requested_keepalive_seconds)})"
        )
        answered = revision.revised_keepalive_seconds or 0.0
        given = f"{revision.revised_max_keepalive_count} ({format_seconds(answered)})"
        window = self.config.supervision.silent_probe_limit
        consequence = ""
        if window is not None:
            direction = (
                "the supervisor asks about silence before this server's keepalive arrives"
                if window < answered
                else "this server speaks before the supervisor asks about the silence"
            )
            consequence = f"; with a silence window of {format_seconds(window)}, {direction}"
        return ConfigFinding(
            FindingSeverity.REVIEW,
            "client.subscriptions.max_keepalive_count",
            f"this client asked for {asked} of silence and this server answered {given}"
            f"{consequence}",
        )

    def _provenance_findings(self) -> tuple[ConfigFinding, ...]:
        """Findings about who wrote a value, which no settings model can see."""
        findings: list[ConfigFinding] = []
        for origin in self.provenance.authored_in(ConfigLayer.FILE):
            if not origin.is_secret:
                continue
            setting = origin.path.rpartition(".")[2]
            findings.append(
                ConfigFinding(
                    FindingSeverity.REVIEW,
                    origin.path,
                    f"this secret is written in {origin.source}: a configuration file is readable "
                    f"by whoever can read the deployment. {ENV_PREFIX}{setting.upper()} keeps it "
                    "out of the file",
                )
            )
        sampling = self.provenance.get("client.subscriptions.sampling_interval")
        if sampling is not None and sampling.layer is ConfigLayer.DERIVED:
            findings.append(
                ConfigFinding(
                    FindingSeverity.REVIEW,
                    "client.subscriptions.sampling_interval",
                    "the sampling interval was derived from the fastest rate this server "
                    "supports, which is the most aggressive choice it offers. A slower number is "
                    "usually what a plant wants, and writing one down is a decision",
                )
            )
        return tuple(findings)

    def _server_findings(self, profile: ServerProfile) -> tuple[ConfigFinding, ...]:
        findings: list[ConfigFinding] = []
        findings.extend(self._namespace_findings(profile))
        findings.extend(self._endpoint_findings(profile))
        findings.extend(self._security_findings(profile))
        findings.extend(self._limit_findings(profile))
        findings.extend(self._sample_rate_findings(profile))
        return tuple(findings)

    def _namespace_findings(self, profile: ServerProfile) -> tuple[ConfigFinding, ...]:
        """Tags whose namespace index this server does not publish."""
        findings: list[ConfigFinding] = []
        published = ", ".join(profile.namespaces) or "none"
        for tag in self.config.tags:
            if tag.node_id is None:
                continue
            index = int(to_nodeid(tag.node_id).NamespaceIndex)
            if index >= len(profile.namespaces):
                findings.append(
                    ConfigFinding(
                        FindingSeverity.REFUSAL,
                        f"tags.{tag.name}.node_id",
                        f"this tag is in namespace {index} and this server publishes "
                        f"{len(profile.namespaces)} namespace(s): {published}. A node cannot be "
                        "monitored in a namespace the server does not have",
                    )
                )
        return tuple(findings)

    def _endpoint_findings(self, profile: ServerProfile) -> tuple[ConfigFinding, ...]:
        """Where the endpoint URL in the configuration came from.

        A discovered endpoint is the Chapter 13 shape and is worth one documentation line rather
        than one config line; a written endpoint that differs from the announced URL of the server
        just asked is worth reading, because an announced URL is a fact about the server, not about
        the network path this client uses.
        """
        authored = self.provenance.get("client.endpoint_url")
        if authored is None:
            return (
                ConfigFinding(
                    FindingSeverity.REVIEW,
                    "client.endpoint_url",
                    f"the endpoint was chosen by discovery ({profile.endpoint_url}) rather than "
                    "written down. That is the Chapter 13 shape, and it deserves one line in the "
                    "deployment's documentation: which server was asked, and when",
                ),
            )
        if self.config.client.endpoint_url != profile.endpoint_url:
            return (
                ConfigFinding(
                    FindingSeverity.REVIEW,
                    "client.endpoint_url",
                    f"the configuration points at {self.config.client.endpoint_url} while the "
                    f"endpoints just discovered are announced from {profile.endpoint_url}: a "
                    "server announces its own URL, which is not necessarily the address a client "
                    "reaches it through",
                ),
            )
        return ()

    def _security_findings(self, profile: ServerProfile) -> tuple[ConfigFinding, ...]:
        """Whether this server publishes the channel the configuration asks for."""
        security = self.config.client.security
        try:
            profile.endpoint_for(security.policy, security.mode)
        except EndpointNotFoundError as exc:
            return (
                ConfigFinding(
                    FindingSeverity.REFUSAL,
                    "client.security",
                    f"{exc}. Build the security settings from what this server offers - "
                    "ServerProfile.security_settings_for() refuses to round an unsupported policy "
                    "down to None",
                ),
            )
        if not security.enabled and not profile.supports_security:
            return (
                ConfigFinding(
                    FindingSeverity.REVIEW,
                    "client.security",
                    "this configuration is unsecured and this server publishes no secured endpoint "
                    "either. Chapter 25 makes that a decision with certificate material behind it "
                    "instead of a default nobody noticed",
                ),
            )
        if not security.enabled:
            return (
                ConfigFinding(
                    FindingSeverity.REVIEW,
                    "client.security",
                    f"this server publishes {len(profile.secured_endpoints)} secured endpoint(s) "
                    "and this configuration uses an unsecured one. That is often the right answer "
                    "on a test rig, and it should be a written-down decision rather than the "
                    "default",
                ),
            )
        return ()

    def _limit_findings(self, profile: ServerProfile) -> tuple[ConfigFinding, ...]:
        """Overrides measured against the limits this server publishes."""
        limits = profile.limits
        if limits is None:
            return (
                ConfigFinding(
                    FindingSeverity.REVIEW,
                    "client.limits",
                    "this server published no operation limits at all; the toolkit assumes a "
                    "conservative value for each, and an override is the only way to say what this "
                    "deployment assumes",
                ),
            )

        findings: list[ConfigFinding] = []
        overrides = self.config.client.limits.as_dict()
        for name in _LIMIT_FIELDS:
            override = overrides.get(name)
            if override is None or name not in limits.reported:
                continue
            published = getattr(limits, name)
            if override > published:
                findings.append(
                    ConfigFinding(
                        FindingSeverity.REFUSAL,
                        f"client.limits.{name}",
                        f"the configuration allows {override} items in one request while this "
                        f"server publishes {published}. An override exists to be more careful than "
                        "the server, not to imagine that it is more generous",
                    )
                )
        unpublished = sorted(field for field in _LIMIT_FIELDS if field not in limits.reported)
        if unpublished:
            findings.append(
                ConfigFinding(
                    FindingSeverity.REVIEW,
                    "client.limits",
                    f"this server does not publish {', '.join(unpublished)}; the toolkit assumes a "
                    "conservative value for each, and an override is how a deployment says what it "
                    "assumes",
                )
            )
        findings.extend(self._chunking_findings(limits))
        return tuple(findings)

    def _chunking_findings(self, limits: OperationLimits) -> tuple[ConfigFinding, ...]:
        """How many service calls the configured tags will need, in the server's own numbers."""
        monitored = [tag for tag in self.config.tags if tag.monitored]
        if not monitored or "max_monitored_items_per_call" not in limits.reported:
            return ()
        per_call = limits.max_monitored_items_per_call
        if len(monitored) <= per_call:
            return ()
        calls = -(-len(monitored) // per_call)
        return (
            ConfigFinding(
                FindingSeverity.REVIEW,
                "tags",
                f"{len(monitored)} monitored item(s) against this server's "
                f"max_monitored_items_per_call={per_call} means the toolkit splits the request "
                f"into {calls} calls. That is Chapter 7 working as intended, but a create that "
                "arrives in pieces is a create that can fail in pieces",
            ),
        )

    def _sample_rate_findings(self, profile: ServerProfile) -> tuple[ConfigFinding, ...]:
        """The configured sampling interval, measured against the floor this server publishes."""
        limits = profile.limits
        if limits is None or limits.min_supported_sample_rate is None:
            return ()
        sample_rate = limits.min_supported_sample_rate
        subscriptions = self.config.subscriptions
        floor = format_milliseconds(sample_rate)
        asked = format_milliseconds(subscriptions.sampling_interval)
        findings: list[ConfigFinding] = []

        if 0.0 < subscriptions.sampling_interval < sample_rate:
            findings.append(
                ConfigFinding(
                    FindingSeverity.REVIEW,
                    "client.subscriptions.sampling_interval",
                    f"this server publishes a sample-rate floor of {floor} and this configuration "
                    f"asks for {asked}. This server does not enforce the floor it publishes - "
                    "Chapter 6 measured that - but a server that does is entitled to revise the "
                    "interval, and a client that never reads the revision will not notice",
                )
            )
        authored = self.provenance.get("client.subscriptions.sampling_interval")
        decided = authored is not None and authored.layer in DECIDING_LAYERS
        if decided and subscriptions.sampling_interval > sample_rate:
            findings.append(
                ConfigFinding(
                    FindingSeverity.REVIEW,
                    "client.subscriptions.sampling_interval",
                    f"this configuration writes {asked} while this server's published floor is "
                    f"{floor}. The written value wins - a decision outranks a discovery - and the "
                    f"gap is {format_milliseconds(subscriptions.sampling_interval - sample_rate)} "
                    "of rate this deployment is not using, which is worth a decision rather than "
                    "a default",
                )
            )
        return tuple(findings)

    def require_startable(self) -> None:
        """Refuse to start on a configuration that contradicts itself.

        Raises:
            ConfigurationError: The review's refusals, as one sentence.
        """
        self.review().require_startable()

    def require_decided(self, *paths: str) -> None:
        """Refuse to start while a named setting is answered by a default or by a server.

        Args:
            paths: Setting paths this deployment must have a human decision for, e.g.
                ``client.session_name``, ``client.application_uri``.

        Raises:
            ConfigurationError: Which of them are still defaults or still discovered. A derived
                value is not a decision either: it is a server answering a question that belongs to
                this application.

        Note:
            A file and an environment variable both count as decided. A deployment that sets
            ``OPCUA_TUTORIAL_CLIENT__SESSION_NAME`` has decided it, in a place an operator can
            change without editing a file.
        """
        undecided = sorted(
            path
            for path in paths
            if (origin := self.provenance.get(path)) is None or not origin.is_decided
        )
        if not undecided:
            return
        raise ConfigurationError(
            "these settings must be decided by this deployment and are not: " + ", ".join(undecided)
        )

    def describe(self) -> tuple[str, ...]:
        """The resolved configuration as report lines: decided values first, then the defaults.

        Both groups are printed as :class:`ResolvedValue` lines - value, declared unit, authored
        form, origin - because a report that shows ``publishing_interval = 2s`` in one group and
        ``2000`` in the other is a report where the reader has to work out which is which.
        """
        values = self.resolved_values()
        authored = tuple(value for value in values if not value.is_default)
        defaults = tuple(value for value in values if value.is_default)
        return (
            f"configuration from: {', '.join(self.sources) or 'defaults only'}",
            f"{len(authored)} value(s) decided by somebody:",
            *(f"  {value.describe()}" for value in authored),
            f"{len(defaults)} value(s) left to defaults:",
            *(f"  {value.describe()}" for value in defaults),
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation for ``opcua-toolkit config --json``."""
        return {
            "sources": list(self.sources),
            "settings": [value.as_dict() for value in self.resolved_values()],
            "review": self.review().as_dict(),
        }


def _read_value(model: BaseModel, path: str) -> Any:
    current: Any = model
    for part in path.split("."):
        current = getattr(current, part)
    return current


def _is_scalar(value: Any) -> bool:
    """Whether a value is one readable token rather than a list or a nested mapping."""
    return not isinstance(value, Mapping | list | tuple | set | frozenset)


def _render(value: Any) -> str:
    """A value as one readable token."""
    if value is None:
        return "not set"
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    if isinstance(value, list | tuple) and value:
        names = [item.name for item in value if isinstance(item, TagSettings)]
        if len(names) == len(value):
            return f"{len(value)} tag(s): {', '.join(names)}"
        named = [item["name"] for item in value if isinstance(item, Mapping) and "name" in item]
        if len(named) == len(value):
            return f"{len(value)} tag(s): {', '.join(str(name) for name in named)}"
        return f"{len(value)} item(s)"
    if isinstance(value, (set, frozenset)):
        return f"{len(value)} item(s)"
    return str(value)


def _json_safe(value: Any) -> Any:
    """Make an authored or effective value JSON-serialisable."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [
            item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in value
        ]
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return value


__all__ = [
    "DECIDING_LAYERS",
    "ENV_PREFIX",
    "ENV_SEPARATOR",
    "ApplicationConfig",
    "ConfigFinding",
    "ConfigLayer",
    "ConfigProvenance",
    "ConfigReview",
    "FindingSeverity",
    "ResolvedConfig",
    "ResolvedValue",
    "SupervisionSettings",
    "TagSettings",
    "ValueOrigin",
    "config_fields",
    "discovery_settings",
    "leaf_fields",
    "load_config",
    "section_paths",
]
