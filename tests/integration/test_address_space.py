"""Chapter 16 integration tests: an authored address space, read back over a session.

These tests run against the tutorial's own server with its opt-in authored namespace, because
the point of Chapter 16 is the round trip: a model is *written* through the AddNodes service,
and then a client that never saw the source code browses it and finds the same promises - the
declarations, the ModellingRules, the access rights, the VariableType. A unit test can prove a
bad spec was refused; only a live server shows that a declaration a client browses really does
say ``Mandatory`` and really does carry ``AccessLevel=0x03``.

Two measured permission rules decide several of these tests, both read out of
``asyncua/server/address_space.py``: a session whose role is not ``Admin`` is refused every
AddNodes (281-283), and a non-Admin write of a ``Value`` needs the ``CurrentWrite`` bit in both
``AccessLevel`` and ``UserAccessLevel`` (94-121). Chapter 25 replaces the tutorial's ``admin``
shortcut with real security; until then these tests are the honest record of what the tutorial
server lets a client do.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from asyncua import Client, ua
from asyncua.ua.uaerrors import UaStatusCodeError

from opcua_toolkit import (
    AddressSpaceAuthor,
    AddressSpaceError,
    AddressSpaceReader,
    BrowsePathError,
    ClientSettings,
    ComponentSpec,
    DemoServer,
    DemoServerConfig,
    InstanceComponent,
    InstanceReport,
    InstanceSpec,
    ModellingRule,
    NamespaceExplorer,
    ObjectTypeSpec,
    VariableAccess,
    format_node_id,
)
from opcua_toolkit.devserver import AUTHORED_FOLDER_NAME, AUTHORED_INSTANCES, AUTHORED_TYPES

pytestmark = pytest.mark.integration

ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin"

#: The parts ``PumpType`` promises an instance, in the order the server reports them: the four
#: declarations of ``PumpType`` itself (three components, then its property), then the three it
#: inherits from ``MachineType`` in the same shape.
PUMP_OWN_PARTS = ("Pressure", "Vibration", "SetPoint", "MaintenanceNote")
PUMP_INHERITED_PARTS = ("Temperature", "Running", "AssetTag")
PUMP_PARTS = PUMP_OWN_PARTS + PUMP_INHERITED_PARTS

#: Which of those parts every instance must have, and which ones OPC 10000-3, 6.4.4.4.2 says an
#: instance may have: ``Vibration`` and ``MaintenanceNote`` are the two an instance can leave out.
PUMP_MANDATORY_PARTS = ("Pressure", "SetPoint", "Temperature", "Running", "AssetTag")
PUMP_OPTIONAL_PARTS = ("Vibration", "MaintenanceNote")


@pytest.fixture
async def authored_server() -> AsyncIterator[DemoServer]:
    """A demo server with the declared model switched on, always shut down."""
    server = DemoServer(DemoServerConfig(port=0, simulate=False, authored_model=True))
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def model_client(connect, authored_server: DemoServer) -> Client:
    """An ordinary, uncredentialed client: it may read everything and author nothing."""
    return await connect(authored_server.endpoint)


@pytest.fixture
async def admin_client(authored_server: DemoServer) -> AsyncIterator[Client]:
    """A client holding the role asyncua's own server requires for AddNodes and node writes.

    asyncua's default user manager answers username ``admin`` with ``UserRole.Admin``
    (``asyncua/server/user_managers.py``, 33-37). That is the tutorial's shortcut, and the
    reason Chapter 25 exists.
    """
    settings = ClientSettings(
        endpoint_url=authored_server.endpoint,
        request_timeout=8.0,
        session_timeout=30.0,
        username=ADMIN_USER,
        password=ADMIN_PASSWORD,
        session_name="Chapter 16 authoring session",
    )
    client = settings.create_client()
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()


def node_id(name: str, server: DemoServer) -> ua.NodeId:
    """One NodeId of the tutorial namespace, so a test does not repeat the index everywhere."""
    return ua.NodeId(name, server.namespace_index)


def part(report: InstanceReport, browse_name: str) -> InstanceComponent:
    """One component of an instance, refusing to compare anything against ``None``."""
    component = report.component(browse_name)
    assert component is not None
    return component


async def folder_child_names(client: Client, folder_id: ua.NodeId) -> list[str]:
    """BrowseNames of the children of the model folder, as a client finds them by browsing."""
    explorer = NamespaceExplorer(client)
    return [child.browse_name.Name for child in await explorer.children(folder_id)]


# ------------------------------------------------------------------------- opt-in


async def test_the_plain_demo_server_keeps_its_authored_namespace_off(
    client, demo_server: DemoServer
) -> None:
    """Chapters 4 to 15 measured a server of plain variables, and that server is the default."""
    assert demo_server.authored_types == {}
    assert demo_server.authored_instances == {}
    assert demo_server.authored_folder_id is None
    assert demo_server.author is None
    explorer = NamespaceExplorer(client)
    with pytest.raises(BrowsePathError, match=AUTHORED_FOLDER_NAME):
        await explorer.find_by_browse_path([AUTHORED_FOLDER_NAME])


async def test_the_authored_server_reports_what_it_authored(authored_server: DemoServer) -> None:
    """A server that wrote its own model can say what it wrote, in reports it read back."""
    assert sorted(authored_server.authored_types) == ["MachineType", "PumpType"]
    assert sorted(authored_server.authored_instances) == ["Pump_01", "Pump_02"]
    folder_id = authored_server.authored_folder_id
    assert folder_id is not None
    assert folder_id.NamespaceIndex == authored_server.namespace_index
    author = authored_server.author
    assert author is not None
    assert author.namespace_index == authored_server.namespace_index


# ------------------------------------------------------------------ the type contract


async def test_an_authored_type_reports_the_declarations_it_inherits(
    model_client, authored_server: DemoServer
) -> None:
    """OPC 10000-3, 6.3.3.2: the hierarchy an instance is built from includes the inherited ones."""
    report = await AddressSpaceReader(model_client).read_type(node_id("PumpType", authored_server))
    assert report.browse_name == "PumpType"
    assert report.instantiable
    assert not report.declares_nothing
    assert report.declared_names == PUMP_PARTS
    assert report.own_declaration_count == len(PUMP_OWN_PARTS)
    inherited = [item for item in report.declarations if item.inherited_from is not None]
    assert [item.browse_name for item in inherited] == list(PUMP_INHERITED_PARTS)
    assert {item.inherited_from for item in inherited} == {"MachineType"}
    assert sorted(report.mandatory_names) == sorted(PUMP_MANDATORY_PARTS)
    assert sorted(report.optional_names) == sorted(PUMP_OPTIONAL_PARTS)
    assert report.never_instantiated == ()


async def test_the_abstract_supertype_is_a_contract_no_instance_of_it_exists(
    model_client, authored_server: DemoServer
) -> None:
    """OPC 10000-3, 5.5.2: "no Objects of this type shall exist, only Objects of its subtypes"."""
    reader = AddressSpaceReader(model_client)
    machine = await reader.find_type("MachineType")
    assert machine.abstract
    assert machine.declared_abstract
    assert not machine.instantiable
    assert machine.declared_names == PUMP_INHERITED_PARTS

    pump = await reader.read_type(node_id("PumpType", authored_server))
    assert pump.supertype_id == machine.node_id


async def test_an_authored_declaration_carries_its_access_and_sampling(
    model_client, authored_server: DemoServer
) -> None:
    """``AccessLevel`` is a Variable attribute, so it belongs on the declaration (5.6.2)."""
    report = await AddressSpaceReader(model_client).read_type(node_id("PumpType", authored_server))
    masks = {item.browse_name: item.access_mask for item in report.declarations}
    writable = VariableAccess.READ_WRITE.mask
    readable = VariableAccess.READ_ONLY.mask
    assert masks["Temperature"] == writable
    assert masks["Pressure"] == writable
    assert masks["SetPoint"] == writable
    assert masks["Running"] == readable
    assert masks["AssetTag"] == readable
    assert masks["Vibration"] == readable
    assert masks["MaintenanceNote"] == readable

    sampling = {item.browse_name: item.min_sampling_interval for item in report.declarations}
    assert sampling["Temperature"] == 100.0
    assert sampling["SetPoint"] == 250.0
    # What the server holds where this author set nothing. OPC 10000-3, 5.6.2 Table 13 gives 0 a
    # meaning rather than leaving it blank: "A MinimumSamplingInterval of 0 indicates that the
    # Server is to monitor the item continuously. A MinimumSamplingInterval of -1 means
    # indeterminate." asyncua creates every Variable at 0 unless the author writes the attribute.
    assert sampling["Pressure"] == 0.0
    assert sampling["MaintenanceNote"] == 0.0

    rules = {item.browse_name: item.rule for item in report.declarations}
    assert rules["Vibration"] is ModellingRule.OPTIONAL
    assert rules["MaintenanceNote"] is ModellingRule.OPTIONAL
    assert rules["Temperature"] is ModellingRule.MANDATORY
    assert all(item.rule_matches_declaration for item in report.declarations)

    initial = {item.browse_name: item.initial_value for item in report.declarations}
    assert initial["Temperature"] == 21.5
    assert initial["AssetTag"] == "UNLABELLED"
    # ``Vibration`` is a Float: 32-bit, so 2.4 arrives as the nearest float32. This is the same
    # 32/64 bit fact Chapter 8 wrote as a ``BadTypeMismatch``, seen from the author's side.
    assert initial["Vibration"] == pytest.approx(2.4, abs=1e-6)


async def test_an_authored_variable_type_is_what_its_component_is_typed_with(
    model_client, authored_server: DemoServer
) -> None:
    """OPC 10000-3, 4.6.1's ``SetPoint`` example: many objects, one shared VariableType."""
    reader = AddressSpaceReader(model_client)
    report = await reader.read_type(node_id("PumpType", authored_server))
    assert len(report.variable_types) == 1
    assert len(report.specialised_variable_types) == 1
    variable_type = report.specialised_variable_types[0]
    assert variable_type.browse_name == "MachineSetPointType"
    assert variable_type.data_type == "Float"
    assert variable_type.value_rank == int(ua.ValueRank.Scalar)
    assert "1 VariableType of their own" in report.summary_line()
    assert "MachineSetPointType" in variable_type.summary_line()

    declaration = report.declaration("SetPoint")
    assert declaration is not None
    assert declaration.specialised_type
    assert declaration.type_definition_id == variable_type.node_id
    assert declaration.type_definition_name == format_node_id(variable_type.node_id)

    read_back = await reader.variable_type_report(variable_type.node_id)
    assert read_back.browse_name == "MachineSetPointType"
    assert read_back.data_type == "Float"
    assert read_back.supertype_id == ua.NodeId(ua.ObjectIds.BaseDataVariableType, 0)
    assert read_back.reused

    with pytest.raises(AddressSpaceError, match="not a VariableType"):
        await reader.variable_type_report(node_id("PumpType", authored_server))


async def test_a_client_finds_a_type_by_name_and_refuses_one_that_only_almost_matches(
    model_client, authored_server: DemoServer
) -> None:
    """OPC 10000-3, 5.2.4: BrowseNames are case sensitive to a client, so a near miss is named."""
    reader = AddressSpaceReader(model_client)
    found = await reader.find_type("PumpType")
    assert found.node_id == node_id("PumpType", authored_server)
    with pytest.raises(AddressSpaceError, match="near miss") as caught:
        await reader.find_type("machinetype", max_browses=600)
    assert "MachineType" in str(caught.value)
    with pytest.raises(AddressSpaceError, match="no ObjectType named"):
        await reader.find_type("NoSuchType", max_browses=40)
    with pytest.raises(AddressSpaceError, match="needs a BrowseName"):
        await reader.find_type("   ")


# ----------------------------------------------------------------------- the instances


async def test_an_instance_holds_every_part_its_type_promises(
    model_client, authored_server: DemoServer
) -> None:
    """The claim of this chapter, measured on a node the test did not author itself."""
    report = await AddressSpaceReader(model_client).describe_instance(
        node_id("Pump_01", authored_server)
    )
    assert report.browse_name == "Pump_01"
    assert report.type_definition_id == node_id("PumpType", authored_server)
    assert report.type_definition_matches
    assert report.conforms_to_type
    assert report.missing_names == ()
    assert report.extra_names == ()
    assert sorted(report.component_names) == sorted(PUMP_PARTS)

    temperature = part(report, "Temperature")
    assert temperature.data_type == "Double"
    assert temperature.access_mask == VariableAccess.READ_WRITE.mask
    assert temperature.is_readable
    assert temperature.is_writable
    assert temperature.min_sampling_interval == 100.0
    assert temperature.value == 61.5
    assert temperature.node_id == node_id("Pump_01.Temperature", authored_server)

    set_point = part(report, "SetPoint")
    assert set_point.data_type == "Float"
    assert set_point.type_definition_name == "ns=2;s=MachineSetPointType"
    assert set_point.min_sampling_interval == 250.0

    note = part(report, "MaintenanceNote")
    assert note.kind.value == "property"
    assert not note.is_writable
    assert note.value == "bearing check due 2026-12-01"

    assert report.type_definition_name == "ns=2;s=PumpType"


async def test_two_instances_of_one_type_differ_only_in_their_values(
    model_client, authored_server: DemoServer
) -> None:
    """OPC 10000-3, 6.4.2: instances inherit the attributes - and the initial values - of a type."""
    reader = AddressSpaceReader(model_client)
    one = await reader.describe_instance(node_id("Pump_01", authored_server))
    two = await reader.describe_instance(node_id("Pump_02", authored_server))
    assert one.component_names == two.component_names
    assert one.conforms_to_type
    assert two.conforms_to_type
    assert part(one, "AssetTag").value == "PUMP-01"
    assert part(two, "AssetTag").value == "PUMP-02"
    assert part(one, "SetPoint").value == 60.0
    assert part(two, "SetPoint").value == 55.0
    assert part(one, "Running").value is not part(two, "Running").value


async def test_optional_parts_can_be_left_out_and_the_instance_still_conforms(
    authored_server: DemoServer, model_client
) -> None:
    """OPC 10000-3, 6.4.4.4.2: an instance *may* have an Optional part, so it may be left out."""
    author = authored_server.author
    assert author is not None
    report = await author.instantiate(
        InstanceSpec(
            name="Pump_03",
            type_name="PumpType",
            include_optional=False,
            values={"SetPoint": 44.0},
        ),
        parent=authored_server.authored_folder_id,
    )
    assert report.conforms_to_type
    assert sorted(report.component_names) == sorted(PUMP_MANDATORY_PARTS)
    assert sorted(report.optional_absent) == sorted(PUMP_OPTIONAL_PARTS)
    assert len(report.components) == len(PUMP_MANDATORY_PARTS)
    assert any("mandatory" in note for note in report.notes)

    read_back = await AddressSpaceReader(model_client).describe_instance(report.node_id)
    assert read_back.conforms_to_type
    assert sorted(read_back.optional_absent) == sorted(PUMP_OPTIONAL_PARTS)
    assert read_back.missing_names == ()


async def test_a_value_for_a_part_this_instance_would_not_have_is_refused_first(
    authored_server: DemoServer, model_client
) -> None:
    """The author refuses before the server is touched, so no half-written instance survives."""
    author = authored_server.author
    assert author is not None
    with pytest.raises(AddressSpaceError, match="not one of them") as caught:
        await author.instantiate(
            InstanceSpec(
                name="Pump_04",
                type_name="PumpType",
                include_optional=False,
                values={"MaintenanceNote": "written anyway"},
            ),
            parent=authored_server.authored_folder_id,
        )
    message = str(caught.value)
    assert "Pump_04" in message
    assert "MaintenanceNote" in message
    assert "leave optional parts out" in message

    folder_id = authored_server.authored_folder_id
    assert folder_id is not None
    assert "Pump_04" not in await folder_child_names(model_client, folder_id)


async def test_an_abstract_type_refuses_an_instance_before_the_server_is_asked(
    authored_server: DemoServer, model_client
) -> None:
    """The refusal names the clause, happens locally, and leaves no stray object in the server."""
    author = authored_server.author
    assert author is not None
    with pytest.raises(AddressSpaceError, match=r"5\.5\.2") as caught:
        await author.instantiate(
            InstanceSpec(name="Machine_01", type_name="MachineType"),
            parent=authored_server.authored_folder_id,
        )
    assert "MachineType" in str(caught.value)
    folder_id = authored_server.authored_folder_id
    assert folder_id is not None
    assert "Machine_01" not in await folder_child_names(model_client, folder_id)


# --------------------------------------------------------------------- writing the values


async def test_the_author_writes_with_the_wire_type_the_component_declares(
    authored_server: DemoServer, model_client
) -> None:
    """``Float`` and ``Double`` are different wire types, and Chapter 8's ``BadTypeMismatch`` is
    what a write that ignores them gets: the wire type is read off the component here."""
    author = authored_server.author
    assert author is not None
    pump_two = node_id("Pump_02", authored_server)
    assert await author.write_component_value(pump_two, "SetPoint", 57.5) == 57.5
    assert await author.write_component_value(pump_two, "Temperature", 42.5) == 42.5
    assert await author.write_component_value(pump_two, "Pressure", 5) == 5.0

    after = await AddressSpaceReader(model_client).describe_instance(pump_two)
    assert part(after, "SetPoint").value == 57.5
    assert part(after, "Temperature").value == 42.5
    assert part(after, "Pressure").value == 5.0


async def test_a_read_only_component_is_refused_locally_before_the_server_could_refuse_it(
    authored_server: DemoServer, model_client
) -> None:
    """The toolkit refuses a write its own declaration says is closed, and names what opens it."""
    author = authored_server.author
    assert author is not None
    pump_one = node_id("Pump_01", authored_server)
    with pytest.raises(AddressSpaceError, match="CurrentWrite") as caught:
        await author.write_component_value(pump_one, "AssetTag", "PUMP-01-RENAMED")
    assert "enforce_access_level=False" in str(caught.value)

    tag = model_client.get_node(node_id("Pump_01.AssetTag", authored_server))
    assert await tag.read_value() == "PUMP-01"

    with pytest.raises(AddressSpaceError, match="no component"):
        await author.write_component_value(pump_one, "NoSuchPart", 1.0)


async def test_an_admin_session_can_write_what_a_declaration_closed(
    authored_server: DemoServer, admin_client
) -> None:
    """Measured in ``asyncua/server/address_space.py``, 94-121: every role but Admin is checked.

    This is the one case where ``enforce_access_level=False`` is honest - the session really does
    hold the role the server requires, and the toolkit does not pretend to enforce something the
    server enforces for nobody else.
    """
    index = authored_server.namespace_index
    author = AddressSpaceAuthor(admin_client, namespace_index=index)
    written = await author.write_component_value(
        node_id("Pump_01", authored_server),
        "AssetTag",
        "PUMP-01-RENAMED",
        enforce_access_level=False,
    )
    assert written == "PUMP-01-RENAMED"
    tag = admin_client.get_node(node_id("Pump_01.AssetTag", authored_server))
    assert await tag.read_value() == "PUMP-01-RENAMED"


async def test_an_anonymous_session_is_refused_the_same_write_with_the_check_off(
    authored_server: DemoServer, model_client
) -> None:
    """Turning the toolkit's own check off does not turn the server's check off."""
    author = AddressSpaceAuthor(model_client, namespace_index=authored_server.namespace_index)
    with pytest.raises(AddressSpaceError, match="BadUserAccessDenied"):
        await author.write_component_value(
            node_id("Pump_01", authored_server),
            "AssetTag",
            "written by nobody",
            enforce_access_level=False,
        )


async def test_the_wrong_wire_type_is_refused_by_the_server(
    authored_server: DemoServer, model_client
) -> None:
    """asyncua's server checks the variant against the node's declared ``DataType``."""
    temperature = model_client.get_node(node_id("Pump_01.Temperature", authored_server))
    with pytest.raises(UaStatusCodeError) as caught:
        await temperature.write_value(ua.Variant("hot", ua.VariantType.String))
    assert "BadTypeMismatch" in str(caught.value)


# ---------------------------------------------------------------------- node management


async def test_an_admin_session_authors_a_type_no_test_wrote(
    authored_server: DemoServer, admin_client, model_client
) -> None:
    """Authoring through a *client* session is how a commissioning tool does it.

    The supertype is named as a BrowseName, so a client that was never told the NodeId of a
    server-owned type can still subtype it - which is what :meth:`AddressSpaceReader.find_type`
    exists for.
    """
    index = authored_server.namespace_index
    author = AddressSpaceAuthor(admin_client, namespace_index=index)
    report = await author.author_object_type(
        ObjectTypeSpec(
            name="FanType",
            supertype="MachineType",
            components=(
                ComponentSpec(
                    name="Speed",
                    data_type="Int32",
                    initial=1500,
                    access=VariableAccess.READ_WRITE,
                    rule=ModellingRule.MANDATORY,
                ),
            ),
        )
    )
    assert report.browse_name == "FanType"
    assert report.instantiable
    assert report.declared_names == ("Speed", *PUMP_INHERITED_PARTS)
    assert report.supertype_id == node_id("MachineType", authored_server)

    instance = await author.instantiate(
        InstanceSpec(name="Fan_01", type_name="FanType", values={"Speed": 1800}),
        parent=authored_server.authored_folder_id,
    )
    assert instance.conforms_to_type
    speed = part(instance, "Speed")
    assert speed.value == 1800
    assert speed.data_type == "Int32"
    assert speed.data_type_id == ua.NodeId(ua.ObjectIds.Int32, 0)

    read_back = await AddressSpaceReader(model_client).read_type(report.node_id)
    assert read_back.declared_names == report.declared_names
    assert await AddressSpaceReader(model_client).find_type("FanType") is not None


async def test_a_second_run_of_the_same_model_reuses_every_node(
    authored_server: DemoServer, model_client
) -> None:
    """A gateway authors its own address space at startup, so a restart is not a re-creation.

    asyncua's server answers AddNodes for a NodeId it already has with ``BadNodeIdExists``
    (``asyncua/server/address_space.py``, 294-296), which is why the author looks each node up
    by NodeId and by BrowseName before asking for it.
    """
    author = authored_server.author
    assert author is not None
    folder_id = authored_server.authored_folder_id
    assert folder_id is not None

    types = await author.author_model(AUTHORED_TYPES)
    assert all(report.reused for report in types)
    assert any("already existed" in note for report in types for note in report.notes)

    instances = await author.instantiate_all(AUTHORED_INSTANCES, parent=folder_id)
    assert all(report.conforms_to_type for report in instances)
    assert any("already had a child named" in note for report in instances for note in report.notes)
    assert sorted(await folder_child_names(model_client, folder_id)) == ["Pump_01", "Pump_02"]

    pump_one = await AddressSpaceReader(model_client).describe_instance(
        node_id("Pump_01", authored_server)
    )
    assert sorted(pump_one.component_names) == sorted(PUMP_PARTS)
    assert len(pump_one.components) == 7


async def test_an_anonymous_session_cannot_author_a_type(
    authored_server: DemoServer, model_client
) -> None:
    """asyncua's server refuses AddNodes for every role but Admin (281-283), before it even
    checks whether the NodeId is already taken."""
    author = AddressSpaceAuthor(model_client, namespace_index=authored_server.namespace_index)
    with pytest.raises(AddressSpaceError, match="BadUserAccessDenied") as caught:
        await author.author_object_type(
            ObjectTypeSpec(
                name="GhostType",
                supertype="MachineType",
                components=(ComponentSpec(name="Level", data_type="Double", initial=1.0),),
            )
        )
    assert "GhostType" in str(caught.value)


async def test_a_deleted_component_shows_up_as_drift_on_the_next_read(
    authored_server: DemoServer, admin_client, model_client
) -> None:
    """Drift is not a theory: delete one mandatory part and the next read reports it."""
    node = admin_client.get_node(node_id("Pump_01.Temperature", authored_server))
    await node.delete(delete_references=True, recursive=False)

    report = await AddressSpaceReader(model_client).describe_instance(
        node_id("Pump_01", authored_server)
    )
    assert not report.conforms_to_type
    assert report.missing_names == ("Temperature",)
    assert "NOT conformant" in report.summary_line()
    assert "Temperature" not in report.component_names
    assert report.component("Temperature") is None


# --------------------------------------------------------------------- coverage report


async def test_coverage_says_how_much_of_the_model_is_typed_as_nothing(
    authored_server: DemoServer, model_client
) -> None:
    """The number a deployment argues about, counted on the subtree the server authored."""
    folder_id = authored_server.authored_folder_id
    assert folder_id is not None
    reader = AddressSpaceReader(model_client)
    coverage = await reader.coverage(folder_id)
    assert coverage.variables == 14
    assert coverage.untyped_variables == 0
    assert coverage.truncated is False
    assert coverage.specialised_variables == 2
    assert coverage.base_only_count == 12
    typed = coverage.usage_for("ns=2;s=MachineSetPointType")
    assert typed is not None
    assert typed.count == 2
    base = coverage.usage_for("BaseDataVariableType")
    assert base is not None
    assert base.base_only
    assert "typed only as a base type" in coverage.summary_line()

    objects = await reader.coverage()
    assert objects.variables >= coverage.variables
    assert objects.browsed_nodes >= coverage.browsed_nodes
