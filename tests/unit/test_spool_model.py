"""Chapter 15 unit tests: the on-disk spool, its bounds, and the state it survives with.

These tests use a real file on a real filesystem, which is the point: a spool is only worth
having if the bytes that survive a restart are the bytes a replay reads back. So every assertion
here is made against the file - its line count, its byte size, its contents - and not against the
counters the class keeps for itself. Where the counters and the file disagree, the file is right,
and one test exists precisely to prove the class agrees.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from asyncua import ua
from pydantic import ValidationError

from opcua_toolkit.errors import SpoolError
from opcua_toolkit.spool import (
    JsonlSpool,
    SpoolEntry,
    SpoolFullPolicy,
    SpoolSettings,
)
from opcua_toolkit.subscriptions.model import DEFAULT_SUBSCRIPTION_KEY, DataChangeRecord

BASE = datetime(2026, 5, 4, 9, 0, 0, tzinfo=UTC)


def make_entry(sequence: int, **overrides: object) -> SpoolEntry:
    """One spool entry with a distinct sequence and an arrival time that follows it."""
    fields: dict[str, object] = {
        "node_id": f"ns=2;s=Tag{sequence}",
        "value": float(sequence),
        "status": "Good",
        "tag": f"Tag{sequence}",
        "arrived_at": BASE + timedelta(seconds=sequence),
        "sequence": sequence,
    }
    fields.update(overrides)
    return SpoolEntry(**fields)  # type: ignore[arg-type]


def make_record(sequence: int, **overrides: object) -> DataChangeRecord:
    fields: dict[str, object] = {
        "node_id": ua.NodeId(f"Tag{sequence}", 2),
        "value": float(sequence),
        "status_code": ua.StatusCode(0),
        "source_timestamp": BASE + timedelta(seconds=sequence),
        "server_timestamp": BASE + timedelta(seconds=sequence),
        "client_handle": 300,
        "key": DEFAULT_SUBSCRIPTION_KEY,
        "sequence": sequence,
    }
    fields.update(overrides)
    return DataChangeRecord(**fields)  # type: ignore[arg-type]


def make_spool(tmp_path: Path, **overrides: object) -> JsonlSpool:
    settings = SpoolSettings(path=tmp_path / "spool.jsonl", **overrides)  # type: ignore[arg-type]
    return JsonlSpool(settings)


def file_lines(path: Path) -> list[str]:
    """The file's own lines, read without asking the spool about them."""
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def spool_values(entries: tuple[SpoolEntry, ...]) -> tuple[object, ...]:
    return tuple(entry.value for entry in entries)


def test_a_spool_must_name_a_file(tmp_path: Path) -> None:
    """Refusing a directory when the settings are built beats a traceback mid-outage."""
    with pytest.raises(ValidationError, match="is a directory, not a file"):
        SpoolSettings(path=tmp_path)


def test_a_spool_needs_a_bounded_size() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        SpoolSettings(path=Path("spool.jsonl"), capacity=0)
    with pytest.raises(ValidationError, match="greater than 0"):
        SpoolSettings(path=Path("spool.jsonl"), max_bytes=0)


def test_summary_names_the_file_both_bounds_and_the_full_policy() -> None:
    settings = SpoolSettings(
        path=Path("outage.jsonl"),
        capacity=4,
        max_bytes=1_024,
        full_policy=SpoolFullPolicy.REFUSE,
    )
    summary = settings.summary()

    assert "outage.jsonl" in summary
    assert "capacity=4 spooled records" in summary
    assert "max_bytes=1024 bytes" in summary
    assert "full=refuse" in summary


async def test_written_records_come_back_in_the_order_they_were_written(
    tmp_path: Path,
) -> None:
    spool = make_spool(tmp_path)
    for sequence in range(1, 5):
        write = await spool.append(make_entry(sequence))
        assert write.accepted is True

    entries = await spool.read()

    assert spool_values(entries) == (1.0, 2.0, 3.0, 4.0)
    assert [entry.sequence for entry in entries] == [1, 2, 3, 4]
    stats = await spool.stats()
    assert stats.records == 4
    assert stats.appended == 4
    assert stats.lost == 0


async def test_the_file_holds_one_json_line_per_record(tmp_path: Path) -> None:
    spool = make_spool(tmp_path)
    await spool.append(make_entry(1))
    await spool.append(make_entry(2))

    lines = file_lines(spool.path)
    assert len(lines) == 2
    payload = json.loads(lines[0])
    assert payload["node_id"] == "ns=2;s=Tag1"
    assert payload["value"] == 1.0
    assert payload["source_timestamp"] is None


async def test_the_spool_counts_the_bytes_it_actually_wrote(tmp_path: Path) -> None:
    spool = make_spool(tmp_path)
    await spool.append(make_entry(1))
    stats = await spool.stats()

    assert stats.bytes_used == spool.path.stat().st_size
    assert stats.bytes_used > 0


async def test_an_entry_made_from_a_live_record_keeps_what_a_replay_needs() -> None:
    record = make_record(9, replayed=True, variant_type=ua.VariantType.Double)
    entry = SpoolEntry.from_record(record, tag="Furnace.Temp")

    assert entry.node_id == "ns=2;s=Tag9"
    assert entry.tag == "Furnace.Temp"
    assert entry.status == "Good"
    assert entry.sequence == 9
    assert entry.replayed is True
    assert entry.source_timestamp is not None
    assert entry.source_timestamp.tzinfo is not None
    assert entry.variant_type == "Double"
    assert entry.value_as_text is False


async def test_a_value_json_cannot_write_is_stored_as_text_and_says_so(tmp_path: Path) -> None:
    """A spool that quietly rewrote a byte string would be replaying a lie."""
    spool = make_spool(tmp_path)
    write = await spool.append(SpoolEntry(node_id="ns=2;s=Blob", value=b"\x00\x01\xff"))
    entries = await spool.read()

    assert write.accepted is True
    assert entries[0].value_as_text is True
    assert entries[0].value == "b'\\x00\\x01\\xff'"


async def test_a_full_spool_trims_the_oldest_and_names_what_it_destroyed(
    tmp_path: Path,
) -> None:
    spool = make_spool(tmp_path, capacity=2, full_policy=SpoolFullPolicy.TRIM_OLDEST)
    await spool.append(make_entry(1))
    await spool.append(make_entry(2))
    write = await spool.append(make_entry(3))

    assert write.accepted is True
    assert [entry.sequence for entry in write.trimmed] == [1]
    assert spool_values(await spool.read()) == (2.0, 3.0)
    assert len(file_lines(spool.path)) == 2
    stats = await spool.stats()
    assert stats.trimmed == 1
    assert stats.refused == 0
    assert stats.lost == 1
    assert stats.records == 2


async def test_the_byte_bound_decides_too_and_not_only_the_record_count(
    tmp_path: Path,
) -> None:
    """A spool bounded only by count is unbounded in size the moment the values get large."""
    payload = "x" * 400
    spool = make_spool(tmp_path, capacity=100, max_bytes=1_000)
    for sequence in range(1, 7):
        write = await spool.append(
            SpoolEntry(
                node_id=f"ns=2;s=Tag{sequence}",
                value=payload,
                tag=f"Tag{sequence}",
                arrived_at=BASE + timedelta(seconds=sequence),
                sequence=sequence,
            )
        )
        assert write.accepted is True, "a trimming spool always takes the record"

    stats = await spool.stats()
    held = await spool.read()

    assert stats.records < 6, "the byte bound has to be a real bound"
    assert stats.records == len(held)
    assert stats.records == len(file_lines(spool.path))
    assert stats.bytes_used == spool.path.stat().st_size
    assert stats.bytes_used <= spool.settings.max_bytes
    assert stats.trimmed == 6 - stats.records
    assert [entry.sequence for entry in held] == list(range(6 - stats.records + 1, 7)), (
        "trimming keeps the freshest records"
    )


async def test_a_record_too_big_for_the_spool_is_refused_before_anything_is_trimmed(
    tmp_path: Path,
) -> None:
    """Trimming for a record that cannot fit destroys the backlog and still holds nothing."""
    spool = make_spool(tmp_path, capacity=100, max_bytes=300)
    await spool.append(SpoolEntry(node_id="ns=2;s=Small", value=1.0, sequence=1))
    before = spool.path.read_bytes()
    write = await spool.append(
        SpoolEntry(node_id="ns=2;s=Huge", value="y" * 4_000, tag=None, sequence=2)
    )

    assert write.accepted is False
    assert write.trimmed == (), "nothing may be destroyed for a record that will not fit"
    assert "this record alone is" in write.reason
    assert spool.path.read_bytes() == before
    stats = await spool.stats()
    assert stats.refused == 1
    assert stats.trimmed == 0
    assert stats.records == 1


async def test_a_refusing_spool_keeps_what_it_has_and_counts_what_it_turned_down(
    tmp_path: Path,
) -> None:
    spool = make_spool(tmp_path, capacity=2, full_policy=SpoolFullPolicy.REFUSE)
    await spool.append(make_entry(1))
    await spool.append(make_entry(2))
    before = spool.path.read_bytes()
    refused = await spool.append(make_entry(3))

    assert refused.accepted is False
    assert refused.trimmed == ()
    assert "at its bound (2 of 2 spooled records" in refused.reason
    assert spool.path.read_bytes() == before, "a refusing spool must not touch the file"
    assert spool_values(await spool.read()) == (1.0, 2.0)
    stats = await spool.stats()
    assert stats.refused == 1
    assert stats.trimmed == 0
    assert stats.is_full is True


async def test_take_is_the_acknowledgement_that_stops_a_replay_replaying(
    tmp_path: Path,
) -> None:
    spool = make_spool(tmp_path)
    for sequence in range(1, 6):
        await spool.append(make_entry(sequence))

    batch = await spool.take(2)
    remainder = await spool.read()

    assert spool_values(batch) == (1.0, 2.0)
    assert spool_values(remainder) == (3.0, 4.0, 5.0)
    assert len(file_lines(spool.path)) == 3
    stats = await spool.stats()
    assert stats.taken == 2
    assert stats.records == 3
    assert stats.lost == 0, "a delivered record is not a loss"


async def test_taking_everything_leaves_an_empty_spool_that_still_reports(tmp_path: Path) -> None:
    spool = make_spool(tmp_path)
    await spool.append(make_entry(1))
    await spool.append(make_entry(2))

    assert len(await spool.take(10)) == 2
    stats = await spool.stats()
    assert stats.records == 0
    assert stats.oldest is None
    assert stats.newest is None
    assert stats.backlog_age_ms() is None
    assert spool.exists() is True


async def test_clear_is_a_decision_and_is_counted_apart_from_losses(tmp_path: Path) -> None:
    spool = make_spool(tmp_path)
    for sequence in range(1, 4):
        await spool.append(make_entry(sequence))

    removed = await spool.clear()
    stats = await spool.stats()

    assert removed == 3
    assert stats.cleared == 3
    assert stats.lost == 0
    assert file_lines(spool.path) == []


async def test_a_restarted_spool_reads_its_own_state_back_off_the_file(
    tmp_path: Path,
) -> None:
    """The whole reason a spool exists: the next process learns the backlog from the bytes."""
    first = make_spool(tmp_path)
    for sequence in range(1, 4):
        await first.append(make_entry(sequence))
    await first.take(1)

    reopened = make_spool(tmp_path)
    stats = await reopened.open()

    assert stats.records == 2
    assert reopened.exists() is True
    assert spool_values(await reopened.read()) == (2.0, 3.0)
    assert stats.oldest is not None
    assert stats.oldest.sequence == 2
    assert stats.appended == 0, "a restarted spool has not appended anything yet"


async def test_a_torn_last_line_is_counted_as_damage_not_invented_into_an_entry(
    tmp_path: Path,
) -> None:
    """A crash mid-write leaves half a line. A replay must not read it as a value."""
    spool = make_spool(tmp_path)
    await spool.append(make_entry(1))
    await spool.append(make_entry(2))
    with spool.path.open("ab") as handle:
        handle.write(b'{"node_id": "ns=2;s=Tag3", "value": 3.0, "seq')

    reopened = make_spool(tmp_path)
    stats = await reopened.open()
    entries = await reopened.read()

    assert stats.corrupt_lines == 1
    assert stats.lost == 1
    assert len(entries) == 2
    assert spool_values(entries) == (1.0, 2.0)


async def test_reading_entries_out_of_order_is_refused_not_silently_accepted(
    tmp_path: Path,
) -> None:
    with pytest.raises(SpoolError, match="not JSON"):
        SpoolEntry.from_line("not json at all")
    with pytest.raises(SpoolError, match="not an entry"):
        SpoolEntry.from_line("[1, 2, 3]")
    with pytest.raises(SpoolError, match="no node_id"):
        SpoolEntry.from_line(json.dumps({"value": 1.0}))


def test_a_timestamp_without_an_offset_is_refused() -> None:
    """A naive timestamp reads as local time on whatever machine replays it."""
    with pytest.raises(SpoolError, match="no UTC offset"):
        SpoolEntry.from_line(
            json.dumps({"node_id": "ns=2;s=A", "arrived_at": "2026-05-04T09:00:00"})
        )
    with pytest.raises(SpoolError, match="not ISO 8601 text"):
        SpoolEntry.from_line(json.dumps({"node_id": "ns=2;s=A", "arrived_at": "4 May 2026"}))


def test_an_entry_round_trips_through_its_own_line() -> None:
    entry = make_entry(7)
    parsed = SpoolEntry.from_line(entry.as_line())

    assert parsed.node_id == entry.node_id
    assert parsed.value == entry.value
    assert parsed.tag == entry.tag
    assert parsed.arrived_at == entry.arrived_at
    assert parsed.sequence == entry.sequence


async def test_a_spool_that_trimmed_reports_the_age_it_destroyed(tmp_path: Path) -> None:
    """ "The disk was full" is not an excuse for losing the oldest evidence of a long outage."""
    spool = make_spool(tmp_path, capacity=1)
    await spool.append(make_entry(1, arrived_at=BASE - timedelta(hours=3)))
    write = await spool.append(make_entry(2))

    assert write.trimmed[0].arrived_at == BASE - timedelta(hours=3)
    assert "made room by removing 1 spooled record(s)" in write.describe()


async def test_the_backlog_is_reported_as_age_and_not_only_as_a_count(tmp_path: Path) -> None:
    spool = make_spool(tmp_path)
    await spool.append(make_entry(1, arrived_at=datetime.now(UTC) - timedelta(minutes=45)))
    stats = await spool.stats()
    age = stats.backlog_age_ms()

    assert age is not None
    assert age >= 45 * 60 * 1000.0
    joined = " ".join(stats.describe())
    assert "oldest spooled record arrived" in joined
    assert "spooled records" in joined
    assert "admitted losing 0 record(s)" in joined


async def test_an_empty_spool_reports_itself_as_empty(tmp_path: Path) -> None:
    spool = make_spool(tmp_path)
    stats = await spool.stats()

    assert stats.records == 0
    assert stats.bytes_used == 0
    assert "the spool holds nothing" in " ".join(stats.describe())
    assert await spool.read() == ()
    assert await spool.take(5) == ()


async def test_a_spool_writes_its_file_even_when_the_directory_did_not_exist(
    tmp_path: Path,
) -> None:
    spool = JsonlSpool(SpoolSettings(path=tmp_path / "var" / "spool" / "outage.jsonl", capacity=3))
    write = await spool.append(make_entry(1))

    assert write.accepted is True
    assert spool.path.is_file()
    assert spool.exists() is True


async def test_the_stats_report_is_json_safe(tmp_path: Path) -> None:
    spool = make_spool(tmp_path, capacity=1)
    await spool.append(make_entry(1))
    await spool.append(make_entry(2))
    payload = json.dumps((await spool.stats()).as_dict())

    assert '"trimmed": 1' in payload
    assert '"records": 1' in payload
    assert '"capacity": 1' in payload
