"""Chapter 16 tests: the address-space model, before any server is asked.

Everything here is decided without a socket. That is deliberate: the rules these specs
enforce are rules a reviewer can check on paper (OPC 10000-3, 4.6.4 forbids a type that
references two Nodes with the same BrowseName; 5.2.4 makes BrowseNames case sensitive;
6.4.4.4.1 says Mandatory means every instance has it), and a server is only ever the second
opinion. A model that a server would have refused is refused here, in one readable line,
before a namespace of a running gateway is involved.

The second half checks the *reports*, because a report is what this chapter sells: if the
numbers in it are wrong - a base type counted as an authored type, an access mask compared
against the enum value instead of the bit - then the chapter teaches a measurement that does
not exist.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from asyncua import ua
from pydantic import ValidationError

from opcua_toolkit import (
    ComponentKind,
    ComponentSpec,
    ConfigurationError,
    DeclarationReport,
    InstanceComponent,
    InstanceReport,
    InstanceSpec,
    ModellingRule,
    ObjectTypeSpec,
    TypeCoverage,
    TypeReport,
    TypeUsage,
    VariableAccess,
    VariableRank,
    VariableTypeReport,
    VariableTypeSpec,
    resolve_data_type,
    type_definition_name,
)
from opcua_toolkit.nodeids import looks_like_node_id_text
from opcua_toolkit.server.model import (
    BASE_TYPE_DEFINITION_IDS,
    DEFAULT_DATA_TYPE_ID,
)

PUMP_TYPE_ID = ua.NodeId("PumpType", 2)
SET_POINT_TYPE_ID = ua.NodeId("MachineSetPointType", 2)


def component(
    name: str = "Temperature",
    **overrides: Any,
) -> ComponentSpec:
    """One declaration, with the defaults a real model would use for it."""
    fields: dict[str, Any] = {
        "name": name,
        "data_type": "Double",
        "access": VariableAccess.READ_WRITE,
        "rule": ModellingRule.MANDATORY,
    }
    fields.update(overrides)
    return ComponentSpec(**fields)


def machine_type(**overrides: Any) -> ObjectTypeSpec:
    """The tutorial's own abstract type: what every machine has and nothing instantiates."""
    fields: dict[str, Any] = {
        "name": "MachineType",
        "abstract": True,
        "components": (component("Temperature"), component("Running", data_type="Boolean")),
        "properties": (component("AssetTag", data_type="String"),),
    }
    fields.update(overrides)
    return ObjectTypeSpec(**fields)


# -------------------------------------------------------------------------------- modules


def test_modelling_rules_point_at_the_nodes_the_specification_defines():
    """Each rule's ``HasModellingRule`` target is the standard node OPC 10000-5 assigns."""
    expected = {
        ModellingRule.MANDATORY: ua.ObjectIds.ModellingRule_Mandatory,
        ModellingRule.OPTIONAL: ua.ObjectIds.ModellingRule_Optional,
        ModellingRule.MANDATORY_PLACEHOLDER: ua.ObjectIds.ModellingRule_MandatoryPlaceholder,
        ModellingRule.OPTIONAL_PLACEHOLDER: ua.ObjectIds.ModellingRule_OptionalPlaceholder,
    }
    for rule, identifier in expected.items():
        node_id = rule.node_id
        assert node_id.NamespaceIndex == 0
        assert node_id.Identifier == identifier


def test_modelling_rules_quote_the_clause_a_reviewer_can_read():
    """A report can name its source, because the clause numbers are part of the model."""
    assert ModellingRule.MANDATORY.spec_clause == "6.4.4.4.1 Mandatory"
    assert ModellingRule.OPTIONAL.spec_clause == "6.4.4.4.2 Optional"
    assert ModellingRule.OPTIONAL_PLACEHOLDER.spec_clause == "6.4.4.4.4 OptionalPlaceholder"
    assert ModellingRule.MANDATORY_PLACEHOLDER.spec_clause == "6.4.4.4.5 MandatoryPlaceholder"


def test_only_mandatory_promises_a_component_every_instance_has():
    """OPC 10000-3, 6.4.4.4.1 versus 6.4.4.4.2: "shall have" versus "may have"."""
    assert ModellingRule.MANDATORY.must_be_born_with_it
    assert not ModellingRule.OPTIONAL.must_be_born_with_it
    assert "shall" in ModellingRule.MANDATORY.promise
    assert "may" in ModellingRule.OPTIONAL.promise


def test_placeholder_rules_are_named_but_not_authored():
    """A reader must be able to say what it sees; this toolkit refuses to write it.

    The placeholder rules (OPC 10000-3, 6.4.4.4.4 and 6.4.4.4.5) describe Nodes an *instance*
    adds on its own. An author who writes one as if it were Mandatory gets a declaration that
    is instantiated like an ordinary one, so the refusal is the honest behaviour.
    """
    assert ModellingRule.MANDATORY_PLACEHOLDER.is_placeholder
    assert ModellingRule.OPTIONAL_PLACEHOLDER.is_placeholder
    assert not ModellingRule.MANDATORY.is_placeholder
    with pytest.raises(ValidationError, match="does not author"):
        component("Slots", rule=ModellingRule.MANDATORY_PLACEHOLDER)
    with pytest.raises(ValidationError, match="does not author"):
        component("Slots", rule=ModellingRule.OPTIONAL_PLACEHOLDER)


def test_component_kind_decides_the_reference_and_the_base_variable_type():
    """``HasComponent`` hangs on a DataVariable, ``HasProperty`` on a Property (4.5.2)."""
    assert ComponentKind.COMPONENT.reference_type_id.Identifier == ua.ObjectIds.HasComponent
    assert ComponentKind.PROPERTY.reference_type_id.Identifier == ua.ObjectIds.HasProperty
    assert ComponentKind.COMPONENT.base_variable_type_id.Identifier == (
        ua.ObjectIds.BaseDataVariableType
    )
    assert ComponentKind.PROPERTY.base_variable_type_id.Identifier == ua.ObjectIds.PropertyType
    # OPC 10000-5, 7.4: a Property's VariableType subtypes PropertyType, not the DataVariable base.
    assert ComponentKind.COMPONENT.default_type_definition == (
        ComponentKind.COMPONENT.base_variable_type_id
    )


def test_access_rights_are_written_as_bit_masks_and_not_as_enum_values():
    """The trap this chapter hit first: ``CurrentRead.value`` is the *bit number* 0.

    asyncua stores ``AccessLevel`` as a bit field, so a mask of 1 means "readable" and the
    enum value 0 means "nothing at all". Writing the values produced variables no client
    could read, which is why the mask is what the model exposes.
    """
    assert VariableAccess.READ_ONLY.mask == 0x01
    assert VariableAccess.READ_WRITE.mask == 0x03
    assert int(ua.AccessLevel.CurrentRead.value) == 0
    assert int(ua.AccessLevel.CurrentRead.mask) == 0x01
    assert int(ua.AccessLevel.CurrentWrite.mask) == 0x02
    assert VariableAccess.READ_ONLY.description == "read only"
    assert VariableAccess.READ_WRITE.description == "read and write"


def test_ranks_are_the_numbers_opc_10000_3_5_6_5_lists():
    """Scalar -1, OneDimension 1, Any -2, and nothing with more dimensions by accident."""
    assert VariableRank.SCALAR.value_rank == int(ua.ValueRank.Scalar) == -1
    assert VariableRank.ONE_DIMENSION.value_rank == int(ua.ValueRank.OneDimension) == 1
    assert VariableRank.ANY.value_rank == int(ua.ValueRank.Any) == -2
    assert VariableRank.SCALAR.description == "one value"
    assert VariableRank.ANY.description == "scalar or array"


# ---------------------------------------------------------------------------- data types


def test_resolve_data_type_accepts_the_specification_spelling():
    """``"Double"`` and ``"double"`` are the same data type: namespace 0, id 11."""
    assert resolve_data_type("Double") == ua.NodeId(ua.ObjectIds.Double, 0)
    assert resolve_data_type("  double  ") == ua.NodeId(ua.ObjectIds.Double, 0)
    assert resolve_data_type("Int32") == ua.NodeId(ua.ObjectIds.Int32, 0)
    assert resolve_data_type("Float") == ua.NodeId(ua.ObjectIds.Float, 0)


def test_resolve_data_type_accepts_a_node_id_in_any_shape():
    """A numeric identifier, NodeId text, or an already-built NodeId all pass through."""
    assert resolve_data_type(ua.ObjectIds.UInt32) == ua.NodeId(ua.ObjectIds.UInt32, 0)
    assert resolve_data_type("11") == ua.NodeId(11, 0)
    assert resolve_data_type("ns=0;i=11") == ua.NodeId(11, 0)
    custom = ua.NodeId(9001, 4)
    assert resolve_data_type(custom) is custom
    assert resolve_data_type("ns=4;i=9001") == custom


def test_resolve_data_type_names_the_data_types_that_are_legal_when_it_refuses():
    """An unknown name is the most common authoring mistake, so the refusal lists the list."""
    with pytest.raises(ConfigurationError) as caught:
        resolve_data_type("Decimal")
    message = str(caught.value)
    assert "'Decimal'" in message
    assert "Double" in message
    assert "Float" in message
    assert "Pass a NodeId instead" in message


def test_a_component_writes_a_data_type_name_and_stores_a_node_id():
    """``DataTypeName``: readable in a config file, canonical in the model.

    The annotation is a validator and a serializer rather than ``str | int | ua.NodeId``,
    because pydantic cannot build a schema for asyncua's wire types: ``ua.NodeId.Identifier``
    is typed ``Int32 | String | Guid | ByteString``, and a plain ``ua.NodeId`` field fails at
    class creation, far from the spec that caused it.
    """
    spec = component("Pressure", data_type="Float")
    assert spec.data_type == ua.NodeId(ua.ObjectIds.Float, 0)
    assert spec.data_type_text == "Float"
    assert spec.model_dump()["data_type"] == "ns=0;i=10"
    assert json.dumps(spec.model_dump(mode="json"))


def test_a_component_keeps_the_data_type_of_a_custom_namespace():
    """A companion specification's data type is legal to declare, and is not second-guessed."""
    spec = component("Torque", data_type="ns=4;i=9001")
    assert spec.data_type == ua.NodeId(9001, 4)


def test_a_component_without_a_data_type_is_a_double():
    """A Python ``float`` is a Double, and a silent 32/64 bit surprise is the one mistake
    this toolkit refuses to make twice after Chapter 8 wrote it as a ``BadTypeMismatch``."""
    assert DEFAULT_DATA_TYPE_ID.Identifier == ua.ObjectIds.Double
    assert DEFAULT_DATA_TYPE_ID.NamespaceIndex == 0
    assert component("Level").data_type == DEFAULT_DATA_TYPE_ID
    assert component("Level").data_type_text == "Double"


def test_a_bad_data_type_is_refused_inside_the_spec_that_wrote_it():
    """The error is a pydantic line on the component, not a traceback three layers away."""
    with pytest.raises(ValidationError) as caught:
        component("Level", data_type="Money")
    assert "is not one of" in str(caught.value)


# --------------------------------------------------------------------------- browse names


@pytest.mark.parametrize(
    "bad_name",
    ["", "   ", "1st", "Bad.Name", "Bad:Name", "Bad Name", "Bad-Name", ".hidden"],
)
def test_browse_names_a_browse_path_could_not_express_are_refused(bad_name: str):
    """OPC 10000-3, 5.8.2 asks for letters, digits and underscores - and asyncua names the
    components of a string-NodeId instance ``<instance>.<browse name>``, so a dot in a name
    makes every NodeId of that instance ambiguous."""
    refused = "BrowsePath|must not be empty"
    for builder in (
        lambda: component(bad_name),
        lambda: machine_type(name=bad_name),
        lambda: InstanceSpec(name=bad_name, type_name="PumpType"),
        lambda: VariableTypeSpec(name=bad_name),
    ):
        with pytest.raises(ValidationError, match=refused):
            builder()


def test_a_browse_name_is_stored_trimmed():
    """Whitespace is a typo, not part of a name, and a name with a space in it is a mystery."""
    assert component(" Temperature ").name == "Temperature"
    assert machine_type(name=" MachineType ").name == "MachineType"


def test_the_base_types_a_report_calls_base_types_are_the_four_the_specification_gives():
    """OPC 10000-3, 4.6.1 allows these as a fallback; OPC 10000-5, 6.2 says avoid them."""
    identifiers = {node_id.Identifier for node_id in BASE_TYPE_DEFINITION_IDS}
    assert identifiers == {
        ua.ObjectIds.BaseObjectType,
        ua.ObjectIds.BaseVariableType,
        ua.ObjectIds.BaseDataVariableType,
        ua.ObjectIds.PropertyType,
    }


def test_type_definition_name_names_what_the_specification_names():
    """A readable name where one is known, the NodeId itself where it is somebody else's."""
    assert type_definition_name(ua.NodeId(ua.ObjectIds.BaseDataVariableType, 0)) == (
        "BaseDataVariableType"
    )
    assert type_definition_name(ua.NodeId(ua.ObjectIds.PropertyType, 0)) == "PropertyType"
    assert type_definition_name(ua.NodeId(ua.ObjectIds.Double, 0)) == "Double"
    assert type_definition_name(PUMP_TYPE_ID) == "ns=2;s=PumpType"
    assert type_definition_name(None) == "nothing (no HasTypeDefinition)"


def test_type_definition_name_names_a_type_this_module_was_never_told_about():
    """The diagnostic Variables every OPC UA server carries, named instead of printed as ids."""
    assert type_definition_name(ua.NodeId(ua.ObjectIds.ServerStatusType, 0)) == "ServerStatusType"
    assert type_definition_name(ua.NodeId(ua.ObjectIds.BuildInfoType, 0)) == "BuildInfoType"
    assert type_definition_name(ua.NodeId(19725, 0)) == "PubSubDiagnosticsCounterType"

    # The one measurement that decides this whole function: Python 3.13 gives every class body
    # a ``__firstlineno__`` attribute, and on this Python its value is 4 - the same integer as
    # ``Int16``. A reverse table built without skipping the dunder names names a data type
    # after a line number.
    assert ua.ObjectIds.Int16 == 4
    assert type_definition_name(ua.NodeId(4, 0)) == "Int16"


# ------------------------------------------------------------------------------ the model


def test_declarations_keep_declaration_order_and_kind():
    """Components are composed, properties describe: the order a client browses in."""
    spec = machine_type()
    kinds = [kind for _declaration, kind in spec.declarations]
    assert kinds == [ComponentKind.COMPONENT, ComponentKind.COMPONENT, ComponentKind.PROPERTY]
    assert spec.declared_names == ("Temperature", "Running", "AssetTag")
    assert spec.mandatory_names == ("Temperature", "Running", "AssetTag")
    assert spec.optional_names == ()
    assert spec.declaration_for("AssetTag") is not None
    assert spec.declaration_for("Pressure") is None


def test_a_type_that_declares_nothing_is_refused():
    """A type with no InstanceDeclarations browses fine and tells a client nothing, which is
    the unreadable address space this chapter exists to fix."""
    with pytest.raises(ValidationError, match="no components and no properties"):
        ObjectTypeSpec(name="UselessType")


def test_a_type_cannot_declare_the_same_browse_name_twice():
    """OPC 10000-3, 4.6.4: a TypeDefinition "shall never reference two Nodes having the
    same BrowseName using forward hierarchical References"."""
    with pytest.raises(ValidationError, match="more than once") as caught:
        machine_type(
            components=(component("Temperature"), component("Pressure"), component("Temperature"))
        )
    assert "Temperature" in str(caught.value)


def test_names_that_differ_only_by_case_are_refused_with_both_names_in_the_message():
    """OPC 10000-3, 5.2.4: a client treats BrowseNames as case sensitive, a server may not."""
    with pytest.raises(ValidationError) as caught:
        machine_type(
            components=(component("Temperature"),),
            properties=(component("temperature", data_type="String"),),
        )
    message = str(caught.value)
    assert "Temperature" in message
    assert "temperature" in message
    assert "case" in message


def test_a_component_cannot_ask_for_a_type_and_author_one_at_the_same_time():
    """Two ways to say what a declaration is typed with is one too many."""
    with pytest.raises(ValidationError, match="pick one"):
        component(
            "SetPoint",
            data_type="Float",
            variable_type=VariableTypeSpec(name="MachineSetPointType", data_type="Float"),
            type_definition=SET_POINT_TYPE_ID,
        )


def test_a_variable_type_may_not_share_the_browse_name_of_its_own_component():
    """A client browsing the type cannot tell the component from its type."""
    with pytest.raises(ValidationError, match="its own BrowseName"):
        component(
            "SetPoint",
            data_type="Float",
            variable_type=VariableTypeSpec(name="SetPoint", data_type="Float"),
        )


def test_a_declared_component_can_be_typed_with_an_existing_variable_type():
    """The 4.6.1 case: many objects pointing at one common ``SetPoint`` VariableType."""
    spec = component("SetPoint", data_type="Float", type_definition=SET_POINT_TYPE_ID)
    assert spec.type_definition == SET_POINT_TYPE_ID
    assert spec.variable_type is None


def test_a_declared_variable_type_subtypes_the_base_its_kind_needs():
    """The spec choice is left to the author, and the base type is decided by the kind."""
    declared = VariableTypeSpec(name="MachineSetPointType", data_type="Float")
    assert declared.data_type == ua.NodeId(ua.ObjectIds.Float, 0)
    assert declared.rank is VariableRank.SCALAR
    assert declared.supertype is None
    assert declared.data_type_text == "Float"
    assert ComponentKind.COMPONENT.base_variable_type_id.Identifier == (
        ua.ObjectIds.BaseDataVariableType
    )


def test_a_sampling_interval_is_positive_or_absent():
    """``MinimumSamplingInterval`` of 0 would claim a fastest rate of never."""
    assert component("Temperature", min_sampling_interval=100.0).min_sampling_interval == 100.0
    assert component("Temperature", min_sampling_interval=None).min_sampling_interval is None
    with pytest.raises(ValidationError):
        component("Temperature", min_sampling_interval=0.0)
    with pytest.raises(ValidationError):
        component("Temperature", min_sampling_interval=-1.0)


def test_specs_are_frozen_and_refuse_invention():
    """A model that can be edited after a server has written it is a model that lies."""
    spec = component("Temperature")
    with pytest.raises(ValidationError, match="frozen"):
        spec.rule = ModellingRule.OPTIONAL
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        machine_type(invented_field=1)


def test_an_instance_is_declared_by_name_type_and_values():
    """The one flat tuple a deployment is written as."""
    instance = InstanceSpec(
        name="Pump_01",
        type_name="PumpType",
        values={"Temperature": 12.5},
        description="the pump the tutorial measured",
    )
    assert instance.include_optional
    assert instance.node_id is None
    assert dict(instance.values) == {"Temperature": 12.5}
    assert instance.description == "the pump the tutorial measured"


def test_an_instance_refuses_values_that_are_not_a_mapping():
    """A tuple of values is a mistake a config file makes, and it should be named."""
    not_a_mapping: Any = [1, 2, 3]
    also_not_a_mapping: Any = "Temperature=12.5"
    allowed_to_be_absent: Any = None
    with pytest.raises(ValidationError, match="must be a mapping"):
        InstanceSpec(name="Pump_01", type_name="PumpType", values=not_a_mapping)
    with pytest.raises(ValidationError, match="must be a mapping"):
        InstanceSpec(name="Pump_01", type_name="PumpType", values=also_not_a_mapping)
    empty_values = InstanceSpec(name="Pump_01", type_name="PumpType", values=allowed_to_be_absent)
    assert empty_values.values == {}


def test_an_instance_may_be_created_with_an_explicit_node_id():
    """A deployment that has to remember NodeIds writes them, and the author keeps them.

    The spec stores what was written: a NodeId text stays text here, and
    :meth:`AddressSpaceAuthor.node_id` is the place that turns it into a
    :class:`asyncua.ua.NodeId` and checks its namespace.
    """
    instance = InstanceSpec(name="Pump_01", type_name="PumpType", node_id="ns=2;s=Pump_01")
    assert instance.node_id == "ns=2;s=Pump_01"
    assert InstanceSpec(name="Pump_01", type_name="PumpType").node_id is None


def test_an_instance_of_a_type_is_written_as_the_pair_it_belongs_to():
    """``supertype`` may name a type authored earlier in the same model, or any NodeId."""
    machine = machine_type()
    pump = ObjectTypeSpec(
        name="PumpType",
        supertype="MachineType",
        components=(component("Pressure"),),
    )
    assert machine.supertype_name is None
    assert pump.supertype_name == "MachineType"
    typed = ObjectTypeSpec(
        name="ValveType",
        supertype="ns=2;s=MachineType",
        components=(component("Open"),),
    )
    assert typed.supertype_name is None
    assert typed.supertype == "ns=2;s=MachineType"
    assert looks_like_node_id_text(typed.supertype)


# ---------------------------------------------------------------------------- the reports


def declaration(
    name: str = "Temperature",
    **overrides: Any,
) -> DeclarationReport:
    """One InstanceDeclaration as a reader would report it."""
    fields: dict[str, Any] = {
        "browse_name": name,
        "kind": ComponentKind.COMPONENT,
        "node_id": ua.NodeId(f"MachineType.{name}", 2),
        "rule": ModellingRule.MANDATORY,
        "type_definition_id": ua.NodeId(ua.ObjectIds.BaseDataVariableType, 0),
        "data_type": "Double",
        "value_rank": -1,
        "access_mask": VariableAccess.READ_WRITE.mask,
        "min_sampling_interval": 100.0,
    }
    fields.update(overrides)
    return DeclarationReport(**fields)


def test_a_declaration_report_says_what_the_server_says_and_what_the_author_wanted():
    """``declared_rule`` sits next to ``rule`` so a difference is visible instead of theoretical."""
    report = declaration()
    assert report.is_mandatory
    assert report.will_be_instantiated
    assert report.rule_matches_declaration
    assert not report.specialised_type

    drift = declaration(
        "SetPoint",
        rule=ModellingRule.OPTIONAL,
        declared_rule=ModellingRule.MANDATORY,
    )
    assert not drift.is_mandatory
    assert not drift.rule_matches_declaration

    orphan = declaration("Legacy", rule=None)
    assert not orphan.will_be_instantiated
    assert "no modelling rule" in orphan.summary_line()


def test_a_declaration_report_reads_its_access_mask_as_a_mask():
    """0x01 readable, 0x03 readable and writable, 0x00 unreadable: the bit, not the enum."""
    read_only = declaration(access_mask=0x01)
    assert read_only.is_readable
    assert not read_only.is_writable
    read_write = declaration(access_mask=0x03)
    assert read_write.is_readable
    assert read_write.is_writable
    unreadable = declaration(access_mask=0x00)
    assert not unreadable.is_readable
    assert not unreadable.is_writable


def test_a_declaration_line_names_the_reference_the_rule_the_type_and_the_access():
    """The line a chapter prints, and the reason "(base type)" is in it."""
    line = declaration().summary_line()
    assert line == (
        "Temperature: component rule=mandatory data=Double "
        "type=BaseDataVariableType (base type) access=0x03 sampling=100 ms"
    )
    typed = declaration(
        "SetPoint",
        data_type="Float",
        type_definition_id=SET_POINT_TYPE_ID,
        specialised_type=True,
        access_mask=VariableAccess.READ_ONLY.mask,
        min_sampling_interval=250.0,
    )
    assert "type=ns=2;s=MachineSetPointType" in typed.summary_line()
    assert "(base type)" not in typed.summary_line()
    assert "access=0x01" in typed.summary_line()

    inherited = declaration("Pressure", inherited_from="PumpType")
    assert inherited.summary_line().endswith("inherited from PumpType")


def test_a_property_declaration_is_reported_as_a_property():
    """``HasProperty`` is metadata about the thing, and the report says which one it is."""
    report = declaration(
        "AssetTag",
        kind=ComponentKind.PROPERTY,
        data_type="String",
        type_definition_id=ua.NodeId(ua.ObjectIds.PropertyType, 0),
    )
    assert "property" in report.summary_line()
    assert report.type_definition_name == "PropertyType"
    assert not report.specialised_type


def json_ready(report: Any) -> dict[str, Any]:
    """Prove a report survives the step a real deployment takes: turning it into JSON."""
    decoded: dict[str, Any] = json.loads(json.dumps(report.as_dict()))
    return decoded


def test_a_declaration_report_is_json_ready():
    payload = json_ready(declaration("SetPoint", rule=ModellingRule.OPTIONAL))
    assert payload["rule"] == "optional"
    assert payload["kind"] == "component"
    assert payload["node_id"] == "ns=2;s=MachineType.SetPoint"
    assert payload["type_definition_id"] == "ns=0;i=63"
    assert payload["access_mask"] == 3


def type_report(**overrides: Any) -> TypeReport:
    """A type as a reader would report it: own declarations plus inherited ones."""
    fields: dict[str, Any] = {
        "node_id": PUMP_TYPE_ID,
        "browse_name": "PumpType",
        "abstract": False,
        "supertype_id": ua.NodeId(ua.ObjectIds.BaseObjectType, 0),
        "declarations": (
            declaration("Pressure"),
            declaration("SetPoint", data_type="Float", type_definition_id=SET_POINT_TYPE_ID),
            declaration("Temperature", inherited_from="MachineType"),
        ),
        "own_declaration_count": 2,
    }
    fields.update(overrides)
    return TypeReport(**fields)


def test_a_type_report_counts_own_and_inherited_declarations_separately():
    """OPC 10000-3, 6.3.3.2's fully-inherited hierarchy is what an instance is built from."""
    report = type_report()
    assert report.declared_names == ("Pressure", "SetPoint", "Temperature")
    assert report.mandatory_names == ("Pressure", "SetPoint", "Temperature")
    assert report.instantiable
    assert not report.declares_nothing
    assert "(1 inherited)" in report.summary_line()
    assert report.declaration("Temperature") is not None
    assert report.declaration("Nope") is None


def test_a_type_that_declares_nothing_is_reported_as_declaring_nothing():
    """``BaseObjectType`` is legal and is the thing OPC 10000-5, 6.2 says a Server should avoid."""
    empty = type_report(declarations=(), own_declaration_count=0)
    assert empty.declares_nothing
    assert "declares nothing" in empty.summary_line()
    assert empty.declared_names == ()


def test_an_abstract_type_is_a_contract_and_not_a_thing():
    """OPC 10000-3, 5.5.2: "no Objects of this type shall exist, only Objects of its subtypes"."""
    abstract = machine_type_report()
    assert abstract.abstract
    assert not abstract.instantiable
    assert "abstract, no instances" in abstract.summary_line()


def machine_type_report() -> TypeReport:
    """The tutorial's abstract type, as the reader reported it after the author wrote it."""
    return TypeReport(
        node_id=ua.NodeId("MachineType", 2),
        browse_name="MachineType",
        abstract=True,
        declared_abstract=True,
        supertype_id=ua.NodeId(ua.ObjectIds.BaseObjectType, 0),
        declarations=(
            declaration("Temperature"),
            declaration("Running", data_type="Boolean", access_mask=0x01),
            declaration(
                "AssetTag",
                kind=ComponentKind.PROPERTY,
                data_type="String",
                access_mask=0x01,
                type_definition_id=ua.NodeId(ua.ObjectIds.PropertyType, 0),
            ),
        ),
        own_declaration_count=3,
    )


def test_a_type_report_distinguishes_an_authored_variable_type_from_a_base_type():
    """Counting ``BaseDataVariableType`` as a type the author wrote is the number that makes
    a review agree with an address space it cannot actually read."""
    authored = VariableTypeReport(
        node_id=SET_POINT_TYPE_ID,
        browse_name="MachineSetPointType",
        data_type="Float",
        value_rank=-1,
        supertype_id=ua.NodeId(ua.ObjectIds.BaseDataVariableType, 0),
    )
    report = type_report(
        variable_types=(
            authored,
            VariableTypeReport(
                node_id=ua.NodeId(ua.ObjectIds.BaseDataVariableType, 0),
                browse_name="BaseDataVariableType",
                data_type="Double",
                value_rank=-1,
                supertype_id=ua.NodeId(ua.ObjectIds.BaseVariableType, 0),
                reused=True,
            ),
        )
    )
    assert len(report.variable_types) == 2
    assert len(report.specialised_variable_types) == 1
    assert report.specialised_variable_types[0].browse_name == "MachineSetPointType"
    assert "1 VariableType of their own" in report.summary_line()
    assert "MachineSetPointType" in authored.summary_line()
    assert "subtype of BaseDataVariableType" in authored.summary_line()
    assert "DataType=Float ValueRank=-1" in authored.summary_line()


def test_a_type_report_lists_the_declarations_no_instantiation_will_create():
    """OPC 10000-3, 6.4.2: "Nodes referenced by the TypeDefinitionNode without a ModellingRule
    do not appear in the instance" - a type with one carries a promise it does not keep."""
    report = type_report(
        declarations=(
            declaration("Pressure"),
            declaration("Legacy", rule=None),
            declaration("Vibration", rule=ModellingRule.OPTIONAL),
        )
    )
    assert [item.browse_name for item in report.never_instantiated] == ["Legacy"]
    assert [item.browse_name for item in report.unspecialised_declarations] == [
        "Pressure",
        "Legacy",
        "Vibration",
    ]
    assert "1 without a modelling rule" in report.summary_line()
    assert report.optional_names == ("Vibration",)


def test_a_type_report_is_json_ready():
    payload = json_ready(machine_type_report())
    assert payload["browse_name"] == "MachineType"
    assert payload["abstract"] is True
    assert payload["instantiable"] is False
    assert payload["supertype_id"] == "ns=0;i=58"
    assert len(payload["declarations"]) == 3


def instance_component(name: str = "Temperature", **overrides: Any) -> InstanceComponent:
    """One component an instance really has, as the reader found it."""
    fields: dict[str, Any] = {
        "browse_name": name,
        "node_id": ua.NodeId(f"Pump_01.{name}", 2),
        "kind": ComponentKind.COMPONENT,
        "type_definition_id": ua.NodeId(ua.ObjectIds.BaseDataVariableType, 0),
        "data_type_id": ua.NodeId(ua.ObjectIds.Double, 0),
        "data_type": "Double",
        "value_rank": -1,
        "access_mask": VariableAccess.READ_WRITE.mask,
        "min_sampling_interval": 100.0,
        "value": 12.5,
    }
    fields.update(overrides)
    return InstanceComponent(**fields)


def test_an_instance_component_reports_the_wire_type_a_write_needs():
    """``data_type_id`` is what tells a writer which wire type a value has to travel as."""
    set_point = instance_component(
        "SetPoint",
        data_type_id=ua.NodeId(ua.ObjectIds.Float, 0),
        data_type="Float",
    )
    assert set_point.data_type == "Float"
    assert set_point.is_writable
    assert set_point.type_definition_name == "BaseDataVariableType"

    lean = instance_component("Vibration", access_mask=0x01, min_sampling_interval=None)
    line = lean.summary_line()
    assert "access=0x01" in line
    assert "sampling=not readable" in line
    assert "value=12.5" in line
    assert not lean.is_writable


def test_the_sampling_text_says_what_the_specification_says_a_zero_means():
    """asyncua leaves every Variable at 0, and OPC 10000-3, 5.6.2 gives that 0 a meaning."""
    continuous = instance_component("Pressure", min_sampling_interval=0.0)
    assert "sampling=continuous (0 ms)" in continuous.summary_line()

    slow = instance_component("Temperature", min_sampling_interval=250.0)
    assert "sampling=250 ms" in slow.summary_line()

    indeterminate = instance_component("Level", min_sampling_interval=-1.0)
    assert "sampling=indeterminate" in indeterminate.summary_line()

    unknown = instance_component("Vibration", min_sampling_interval=None)
    assert "sampling=not readable" in unknown.summary_line()


def instance_report(**overrides: Any) -> InstanceReport:
    """Pump_01 as the reader described it: typed as PumpType, its mandatory parts present."""
    fields: dict[str, Any] = {
        "node_id": ua.NodeId("Pump_01", 2),
        "browse_name": "Pump_01",
        "type_definition_id": PUMP_TYPE_ID,
        "expected_type_id": PUMP_TYPE_ID,
        "components": (
            instance_component("Temperature"),
            instance_component("Pressure"),
            instance_component("Running", data_type="Boolean"),
        ),
        "declared_names": ("Temperature", "Pressure", "Running", "Vibration"),
        "mandatory_names": ("Temperature", "Pressure", "Running"),
        "optional_absent": ("Vibration",),
    }
    fields.update(overrides)
    return InstanceReport(**fields)


def test_an_instance_is_conformant_when_its_type_is_right_and_no_mandatory_part_is_missing():
    report = instance_report()
    assert report.type_definition_matches
    assert report.conforms_to_type
    assert report.missing_names == ()
    assert "conformant" in report.summary_line()
    assert "NOT conformant" not in report.summary_line()
    assert report.component_names == ("Temperature", "Pressure", "Running")
    assert report.component("Pressure") is not None
    assert report.component("Vibration") is None
    assert report.optional_absent == ("Vibration",)


def test_a_missing_mandatory_part_is_reported_as_drift():
    """A client that assumes a mandatory component gets ``BadBrowseNameNotFound`` from its own
    BrowsePath instead of a warning, which is why the report lists it first."""
    drift = instance_report(
        components=(instance_component("Temperature"),),
        missing_names=("Pressure", "Running", "Vibration"),
        optional_absent=(),
    )
    assert not drift.conforms_to_type
    assert "NOT conformant" in drift.summary_line()
    assert "1 of 4 declared parts" in drift.summary_line()
    assert drift.mandatory_names == ("Temperature", "Pressure", "Running")
    assert list(drift.missing_names) == ["Pressure", "Running", "Vibration"]


def test_extra_components_are_listed_without_by_themselves_breaking_conformance():
    """OPC 10000-3, 6.4.4.4.4 and 6.4.4.4.5 exist exactly for types that expect instances to
    add parts, so "extra" is a finding for a reviewer, not a verdict."""
    extended = instance_report(extra_names=("Speed",))
    assert extended.conforms_to_type
    assert extended.extra_names == ("Speed",)
    assert "extra ['Speed']" in extended.summary_line()


def test_an_instance_typed_with_the_wrong_type_is_reported_as_drift():
    """The single claim this chapter is about: was this node really typed with that type?"""
    drift = instance_report(
        expected_type_id=ua.NodeId("MachineType", 2),
        type_definition_matches=False,
    )
    assert not drift.type_definition_matches
    assert not drift.conforms_to_type
    assert drift.type_definition_name == "ns=2;s=PumpType"


def test_an_optional_part_left_out_is_reported_and_not_counted_as_drift():
    """OPC 10000-3, 6.4.4.4.2: an instance may have it, so leaving it out is legal."""
    lean = instance_report(
        components=(instance_component("Temperature"), instance_component("Pressure")),
        declared_names=("Temperature", "Pressure"),
        mandatory_names=("Temperature", "Pressure"),
        optional_absent=("Vibration",),
    )
    assert lean.conforms_to_type
    assert lean.optional_absent == ("Vibration",)


def test_an_instance_report_is_json_ready():
    payload = json_ready(instance_report())
    assert payload["type_definition_id"] == "ns=2;s=PumpType"
    assert payload["conforms_to_type"] is True
    assert payload["components"][0]["value"] == 12.5


# ----------------------------------------------------------------------------- coverage


def coverage(**overrides: Any) -> TypeCoverage:
    """The AssetModel subtree the tutorial measured before any authored type existed."""
    fields: dict[str, Any] = {
        "start_name": "AssetModel",
        "variables": 14,
        "untyped_variables": 0,
        "usage": (
            TypeUsage(
                type_id=ua.NodeId(ua.ObjectIds.BaseDataVariableType, 0),
                type_name="BaseDataVariableType",
                count=8,
                base_only=True,
            ),
            TypeUsage(
                type_id=ua.NodeId(ua.ObjectIds.PropertyType, 0),
                type_name="PropertyType",
                count=4,
                base_only=True,
            ),
            TypeUsage(
                type_id=SET_POINT_TYPE_ID,
                type_name="ns=2;s=MachineSetPointType",
                count=2,
                base_only=False,
            ),
        ),
        "browsed_nodes": 40,
    }
    fields.update(overrides)
    return TypeCoverage(**fields)


def test_coverage_separates_the_types_that_say_something_from_the_ones_that_do_not():
    """The number a deployment argues about: how much of an address space is typed as nothing."""
    report = coverage()
    assert report.base_only_count == 12
    assert report.specialised_variables == 2
    assert report.base_only_share == pytest.approx(12 / 14)
    assert "2 with a type that says more" in report.summary_line()
    assert "86%" in report.summary_line()


def test_a_truncated_walk_is_reported_as_a_floor_and_not_a_total():
    """A report that silently stops is worse than one that says how far it looked."""
    report = coverage(truncated=True)
    assert "truncated" in report.summary_line()


def test_a_coverage_row_names_its_own_share_of_the_subtree():
    """The usage table, and the mark on the rows that say nothing about the node."""
    report = coverage()
    typed = report.usage_for("ns=2;s=MachineSetPointType")
    assert typed is not None
    assert typed.summary_line() == "   2 variable(s) typed ns=2;s=MachineSetPointType"
    base = report.usage_for("PropertyType")
    assert base is not None
    assert "<- says nothing about the node" in base.summary_line()
    assert report.usage_for("NoSuchType") is None


def test_a_subtree_with_no_variables_reports_a_share_of_zero_and_not_a_crash():
    """A division by zero in a report is how a review chapter becomes a bug report."""
    empty = coverage(variables=0, untyped_variables=0, usage=(), browsed_nodes=1)
    assert empty.base_only_share == 0.0
    assert empty.specialised_variables == 0
    assert "0%" in empty.summary_line()
    assert "0 variables, 0 with no type" in empty.summary_line()


def test_a_coverage_report_is_json_ready():
    payload = json_ready(coverage())
    assert payload["base_only_count"] == 12
    assert payload["specialised_variables"] == 2
    assert payload["usage"][2]["type_id"] == "ns=2;s=MachineSetPointType"
