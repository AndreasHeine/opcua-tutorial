"""Chapter 8 integration tests: writing, and what a write really returns.

The three failure modes every OPC UA client meets are exercised here against a
live server: a node that may not be written, a value that does not match the
node's data type, and a node that does not exist. Each one must produce its own
StatusCode, and none of them may lose track of an item.
"""

from __future__ import annotations

import math

import pytest
from asyncua import ua

from opcua_toolkit.errors import PartialFailureError, format_node_id
from opcua_toolkit.read import BulkReader
from opcua_toolkit.write import BulkWriter, WriteItem, WriteReport

pytestmark = pytest.mark.integration


async def test_a_bulk_write_sends_one_request_per_chunk(
    client, limits, demo_server, service_calls
) -> None:
    batch = demo_server.tag_ids()
    items = [WriteItem(node_id=node_id, value=index + 100) for index, node_id in enumerate(batch)]
    expected_requests = math.ceil(len(batch) / limits.max_nodes_per_write)

    service_calls["write_attributes"] = 0
    report = await BulkWriter(client, limits=limits).write(items)

    assert service_calls["write_attributes"] == expected_requests
    assert report.requests == expected_requests
    assert report.requested == len(batch)
    assert report.succeeded == len(batch)
    assert report.failed == 0
    assert report.unaccounted == 0


async def test_written_values_really_arrive(client, limits, demo_server) -> None:
    batch = demo_server.tag_ids()
    written = {format_node_id(node_id): 1_000 + index for index, node_id in enumerate(batch)}
    report = await BulkWriter(client, limits=limits).write(
        [WriteItem(node_id=node_id, value=value) for node_id, value in written.items()]
    )
    assert report.failed == 0

    # A read back is keyed by NodeId text, because a bulk read is answered per node
    # and an index would silently reorder things if a node failed.
    values = await BulkReader(client, limits=limits).values_of(batch)
    assert {key: values[key] for key in written} == written


async def test_a_write_report_has_one_outcome_per_requested_item(client, limits) -> None:
    items = [
        WriteItem(node_id="ns=2;s=BatchTag002", value=1),
        WriteItem(node_id="ns=2;s=Vibration", value=9.9),
        WriteItem(node_id="ns=2;s=Setpoint", value="not a number"),
        WriteItem(node_id="ns=2;s=NoSuchTag", value=1),
        WriteItem(node_id="ns=2;s=BatchTag003", value=2),
    ]
    report = await BulkWriter(client, limits=limits).write(items)

    assert report.requested == len(items)
    assert len(report.outcomes) == len(items)
    assert report.unaccounted == 0
    assert "MISSING_OUTCOMES" not in report.summary()
    assert report.succeeded == 2
    assert report.failed == 3
    assert report.skipped == 2  # the access check and the missing node never sent a value


async def test_outcomes_keep_the_order_the_caller_sent(client, limits) -> None:
    wanted = ["BatchTag004", "Vibration", "BatchTag005"]
    report = await BulkWriter(client, limits=limits).write(
        [(f"ns=2;s={name}", 7) for name in wanted]
    )
    assert [outcome.node_id_text for outcome in report.outcomes] == [
        f"ns=2;s={name}" for name in wanted
    ]


async def test_a_read_only_node_is_refused_before_the_request_is_sent(client, limits) -> None:
    report = await BulkWriter(client, limits=limits).write([("ns=2;s=Vibration", 9.9)])
    outcome = report.outcomes[0]
    assert outcome.status.name == "BadNotWritable"
    assert "AccessLevel" in outcome.note
    assert "CurrentWrite" in outcome.note
    assert report.skipped == 1


async def test_a_read_only_write_costs_no_service_call(client, limits, service_calls) -> None:
    service_calls["write_attributes"] = 0
    report = await BulkWriter(client, limits=limits).write([("ns=2;s=Vibration", 9.9)])
    assert service_calls["write_attributes"] == 0
    assert report.requests == 0
    assert report.failed == 1


async def test_turning_the_preflight_off_sends_the_write_and_reports_the_verdict(
    client, limits, demo_server, service_calls
) -> None:
    # AccessLevel is something a server *publishes*; enforcing it is a separate decision the
    # server makes for itself. asyncua's server does enforce it, but answers with its own
    # code instead of the one the client's preflight would have produced. A permissive
    # vendor server can even answer Good. So this test does not pin which verdict asyncua
    # gives; it pins the part that is always true: with the preflight off the request really
    # goes out, nothing is refused locally, and the report carries the server's own answer.
    name = "TemperatureHistory_RO"
    before = await demo_server.read_value(name)
    service_calls["write_attributes"] = 0
    service_calls["read_attributes"] = 0
    report = await BulkWriter(client, limits=limits, check_access=False).write(
        [(demo_server.tags[name], 12.5)]
    )
    unchecked_reads = service_calls["read_attributes"]
    assert report.skipped == 0
    assert report.requests >= 1
    assert service_calls["write_attributes"] == report.requests
    assert report.outcomes[0].status.name in {"Good", "BadNotWritable", "BadUserAccessDenied"}
    assert report.outcomes[0].note  # the report keeps the reason, not only the code
    if report.succeeded:
        assert await demo_server.read_value(name) == pytest.approx(12.5)
    await demo_server.set_value(name, before, ua.VariantType.Double)

    # The preflight version still needs the data type for its coercion, so it reads
    # three attributes where the unchecked version reads one - and then it sends nothing.
    service_calls["read_attributes"] = 0
    service_calls["write_attributes"] = 0
    checked = await BulkWriter(client, limits=limits).write([(demo_server.tags[name], 12.5)])
    assert checked.skipped == 1
    assert checked.requests == 0
    assert service_calls["write_attributes"] == 0
    assert service_calls["read_attributes"] == 3 * unchecked_reads


async def test_access_level_splits_the_demo_tags_into_writable_and_read_only(
    client, limits, demo_server
) -> None:
    writable = demo_server.writable_tag_ids()
    readonly = demo_server.readonly_tag_ids()
    assert writable
    assert readonly
    assert not (set(writable) & set(readonly))

    accepted = await BulkWriter(client, limits=limits).write(
        [(node_id, 42) for node_id in demo_server.tag_ids()]
    )
    assert accepted.succeeded == len(demo_server.tag_ids())

    refused = await BulkWriter(client, limits=limits).write([(node_id, 42) for node_id in readonly])
    assert refused.failed == len(readonly)
    assert {outcome.status.name for outcome in refused.failures()} == {"BadNotWritable"}


async def test_a_value_that_does_not_match_the_data_type_is_reported(client, limits) -> None:
    report = await BulkWriter(client, limits=limits).write([("ns=2;s=Setpoint", "not a number")])
    outcome = report.outcomes[0]
    assert outcome.status.name == "BadTypeMismatch"
    assert "not numeric" in outcome.note
    assert "Float" in outcome.note


async def test_an_integer_written_to_a_float_node_is_accepted_after_coercion(
    client, limits
) -> None:
    report = await BulkWriter(client, limits=limits).write([("ns=2;s=Setpoint", 21)])
    assert report.succeeded == 1
    values = await BulkReader(client, limits=limits).values_of(["ns=2;s=Setpoint"])
    assert values["ns=2;s=Setpoint"] == pytest.approx(21.0)


async def test_a_node_that_does_not_exist_is_reported_as_missing_not_as_unwritable(
    client, limits
) -> None:
    report = await BulkWriter(client, limits=limits).write([("ns=2;s=NoSuchTag", 1)])
    outcome = report.outcomes[0]
    assert outcome.status.name == "BadNodeIdUnknown"
    assert report.skipped == 1
    assert "not readable" in outcome.note


async def test_a_number_outside_the_range_of_the_node_is_refused_before_the_write(
    client, limits
) -> None:
    # MotorRpm is UInt32: a negative RPM is a bug in the caller, not in the server.
    report = await BulkWriter(client, limits=limits).write([("ns=2;s=MotorRpm", -5)])
    outcome = report.outcomes[0]
    assert outcome.status.name == "BadTypeMismatch"
    assert "does not fit in UInt32" in outcome.note


async def test_a_write_uses_the_nodes_own_data_type_unless_the_caller_overrides_it(
    client, limits
) -> None:
    report = await BulkWriter(client, limits=limits).write(
        [
            WriteItem(
                node_id="ns=2;s=RecipeName", value="explicit", variant_type=ua.VariantType.String
            ),
            WriteItem(node_id="ns=2;s=MotorRpm", value=1_800, variant_type=ua.VariantType.UInt32),
        ]
    )
    assert report.succeeded == 2
    values = await BulkReader(client, limits=limits).values_of(
        ["ns=2;s=RecipeName", "ns=2;s=MotorRpm"]
    )
    assert values["ns=2;s=RecipeName"] == "explicit"
    assert values["ns=2;s=MotorRpm"] == 1_800


async def test_raise_for_failures_stops_a_collector_from_ignoring_a_bad_batch(
    client, limits
) -> None:
    report = await BulkWriter(client, limits=limits).write(
        [("ns=2;s=BatchTag006", 1), ("ns=2;s=Vibration", 2)]
    )
    with pytest.raises(PartialFailureError, match="ns=2;s=Vibration=BadNotWritable") as caught:
        report.raise_for_failures()
    assert len(caught.value.failures) == 1


async def test_a_clean_report_passes_raise_for_failures_unchanged(client, limits) -> None:
    report = await BulkWriter(client, limits=limits).write([("ns=2;s=BatchTag007", 3)])
    assert isinstance(report.raise_for_failures(), WriteReport)
    assert report.unaccounted == 0


async def test_the_server_can_write_its_own_read_only_tag_but_a_client_cannot(
    client, limits, demo_server
) -> None:
    # AccessLevel guards the Write *service*; it does not constrain the server's own code.
    await demo_server.set_value("Vibration", 3.5, ua.VariantType.Float)
    assert await demo_server.read_value("Vibration") == pytest.approx(3.5)

    report = await BulkWriter(client, limits=limits).write([("ns=2;s=Vibration", 4.5)])
    assert report.outcomes[0].status.name == "BadNotWritable"


async def test_writing_an_array_tag_with_a_list_works(client, limits, service_calls) -> None:
    service_calls["write_attributes"] = 0
    report = await BulkWriter(client, limits=limits).write(
        [
            WriteItem(
                node_id="ns=2;s=SampleArray",
                value=[1.0, 2.0, 3.0],
                variant_type=ua.VariantType.Double,
            )
        ]
    )
    assert report.succeeded == 1
    values = await BulkReader(client, limits=limits).values_of(["ns=2;s=SampleArray"])
    assert values["ns=2;s=SampleArray"] == [1.0, 2.0, 3.0]
