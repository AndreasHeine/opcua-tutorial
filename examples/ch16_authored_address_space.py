"""Chapter 16: authoring an address space, and reading back what it promises.

Every chapter so far read an address space somebody else wrote. This one writes one, and then
reads it the way a client that never saw the source code reads it - because that is the only
version of the model that matters: what a server answers when a client asks what a node *is*.

Four things are measured here, in this order:

1. **What a server says about its own types**: BrowseName, ``HasSubtype``, ``HasTypeDefinition``,
   the modelling rule on each InstanceDeclaration, and what the same declaration says once a
   server has used it to create an instance.
2. **Authoring through a session**: :class:`~opcua_toolkit.AddressSpaceAuthor` pointed at a
   *client* node, so a type is created through the AddNodes service the way a commissioning tool
   creates it, and not by a script that happens to own the server process.
3. **What a declaration promises an instance**: ``Mandatory`` versus ``Optional``, and the
   components the instance really has when the server is asked.
4. **What that contract then costs a client**: which writes this toolkit refuses before the
   request is sent, and which ones the server refuses after it.

Run it:

    uv run python examples/ch16_authored_address_space.py --demo
    uv run python examples/ch16_authored_address_space.py --endpoint opc.tcp://127.0.0.1:4840

With ``--demo`` the tutorial server starts with its opt-in authored namespace (the
``--authored-model`` flag of ``opcua-tutorial-server`` does the same thing) and the example authors
one more type of its own over a client session. Pointed at a real server the authoring half is
skipped - it would write into somebody's plant - and everything a reader can learn from a server it
does not own still runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from asyncua import ua

from _support import bullet, chapter_run, heading
from opcua_toolkit import (
    AddressSpaceAuthor,
    AddressSpaceError,
    AddressSpaceReader,
    BrowsePathError,
    ComponentSpec,
    InstanceReport,
    InstanceSpec,
    ModellingRule,
    NamespaceExplorer,
    ObjectTypeSpec,
    TypeReport,
    VariableAccess,
    VariableTypeSpec,
    format_node_id,
)

#: The folder this chapter reads and, on the tutorial server, writes into. A tutorial model
#: belongs in a tutorial namespace: writing Objects into a production server's ``Objects`` folder
#: is how a tutorial turns into an outage.
MODEL_FOLDER_NAME = "AssetModel"

#: The name the BrowsePath section walks, and the component it must reach. Both are found by
#: browsing first: a path is only a fact after the server has confirmed each segment.
INSTANCE_NAME_FOR_PATH = "Pump_01"
COMPONENT_NAME_FOR_PATH = "Temperature"


def report_type(report: TypeReport) -> None:
    """Print one object type the way the server described it, declarations and notes included."""
    bullet(report.summary_line())
    for note in report.notes:
        bullet(f"   note: {note}")
    for declaration in report.declarations:
        bullet(f"   decl {declaration.summary_line()}")
    for variable_type in report.variable_types:
        bullet(f"   variable type: {variable_type.summary_line()}")


def report_instance(report: InstanceReport) -> None:
    """Print one instance the way the server holds it, against what its type promised."""
    bullet(report.summary_line())
    for note in report.notes:
        bullet(f"   note: {note}")
    for component in report.components:
        bullet(f"   part {component.summary_line()}")


async def show_coverage(reader: AddressSpaceReader, start: Any, label: str) -> int:
    """Print the coverage report for one subtree and return the variable count it reached."""
    coverage = await reader.coverage(start)
    bullet(f"{label}: {coverage.summary_line()}")
    for usage in coverage.usage:
        bullet(f"   {usage.summary_line()}")
    return coverage.variables


async def attempt(label: str, step: Coroutine[Any, Any, Any]) -> None:
    """Run one step that may be refused, and print the refusal instead of stopping the run."""
    try:
        bullet(f"{label}: {await step}")
    except Exception as exc:
        bullet(f"{label} refused: {type(exc).__name__}: {exc}")


async def main() -> int:
    """Run every section of Chapter 16 and print what the server answered to each one."""
    async with chapter_run(
        "Author an address space, and read back what it promises.",
        session_name="Chapter 16 address space",
        simulate=False,
        authored_model=True,
    ) as run:
        # A tutorial server hands the Admin role to the username "admin" and ignores the password,
        # which is the tutorial's own shortcut and Chapter 25's reason to replace it.
        admin_settings = run.settings_for(username="admin", password="admin")

        # ---------------------------------------------------------------- 1
        heading("1. The namespaces, and the model folder the server already has")
        model_folder_id: Any = None
        async with run.open_client() as client:
            explorer = NamespaceExplorer(client)
            for index, uri in await explorer.namespaces():
                bullet(f"ns={index}: {uri}")
            limits = await run.read_limits(client)
            bullet(f"MaxNodesPerRead={limits.max_nodes_per_read}")
            bullet(f"MaxNodesPerMethodCall={limits.max_nodes_per_method_call}")
            reader = AddressSpaceReader(client)
            before = await show_coverage(reader, ua.ObjectIds.ObjectsFolder, "Objects folder")
            try:
                folder = await explorer.find_by_browse_path([MODEL_FOLDER_NAME])
                model_folder_id = folder.node_id
                bullet(f"{MODEL_FOLDER_NAME} is {format_node_id(folder.node_id)}")
            except BrowsePathError as exc:
                bullet(f"no {MODEL_FOLDER_NAME} on this server: {exc}")

        # ---------------------------------------------------------------- 2
        heading("2. A type the example did not author, read the way a client reads it")
        pump_type_id: Any = None
        if model_folder_id is None:
            bullet(f"skipped: this server has no {MODEL_FOLDER_NAME} folder to describe")
        else:
            async with run.open_client() as client:
                explorer = NamespaceExplorer(client)
                reader = AddressSpaceReader(client)
                children = await explorer.children(model_folder_id)
                for child in children:
                    bullet(
                        f"found {child.node_class.name} {child.browse_name.Name} "
                        f"at {format_node_id(child.node_id)}"
                    )
                instances = [child for child in children if child.node_class is ua.NodeClass.Object]
                for child in instances[:2]:
                    instance = await reader.describe_instance(child.node_id)
                    report_instance(instance)
                    if instance.type_definition_id is None:
                        bullet(
                            f"{child.browse_name} has no HasTypeDefinition: "
                            "the server says nothing about what it is"
                        )
                        continue
                    if pump_type_id is None:
                        pump_type_id = instance.type_definition_id
                    report_type(await reader.read_type(instance.type_definition_id))
            async with run.open_client() as client:
                reader = AddressSpaceReader(client)
                for name in ("MachineType", "machinetype", "NoSuchType"):
                    try:
                        found = await reader.find_type(name)
                        bullet(f"find_type({name!r}) -> {format_node_id(found.node_id)}")
                    except AddressSpaceError as exc:
                        bullet(f"find_type({name!r}) refused: {exc}")

        # ---------------------------------------------------------------- 3
        heading("3. Authoring a type over a client session")
        if not run.uses_demo_server:
            bullet("skipped: authoring writes into the server")
            bullet("--endpoint is somebody else's plant, and this chapter will not write into it")
        else:
            index = run.demo_server.namespace_index
            async with run.open_client(settings=admin_settings) as client:
                author = AddressSpaceAuthor(
                    client.get_node(ua.ObjectIds.ObjectsFolder), namespace_index=index
                )
                spec = ObjectTypeSpec(
                    name="FanType",
                    supertype="MachineType",
                    description="A fan: a machine whose speed an operator may change",
                    components=(
                        ComponentSpec(
                            name="Speed",
                            data_type="Int32",
                            initial=1500,
                            access=VariableAccess.READ_WRITE,
                            rule=ModellingRule.MANDATORY,
                            variable_type=VariableTypeSpec(
                                name="FanSpeedType",
                                data_type="Int32",
                                description="Rotations per minute, written by an operator",
                            ),
                        ),
                        ComponentSpec(
                            name="FaultCount",
                            data_type="UInt32",
                            initial=0,
                            rule=ModellingRule.OPTIONAL,
                        ),
                    ),
                    properties=(
                        ComponentSpec(
                            name="Vendor",
                            data_type="String",
                            initial="unknown",
                            rule=ModellingRule.OPTIONAL,
                        ),
                    ),
                )
                authored = await author.author_object_type(spec)
                report_type(authored)
                bullet(
                    f"authored types this session knows: {', '.join(sorted(author.authored_types))}"
                )

                # ------------------------------------------------------------ 4
                heading("4. Instances: the same type with, and without, its optional parts")
                parent = model_folder_id if model_folder_id is not None else None
                fan = await author.instantiate(
                    InstanceSpec(name="Fan_01", type_name="FanType", values={"Speed": 1800}),
                    parent=parent,
                )
                report_instance(fan)
                lean = await author.instantiate(
                    InstanceSpec(
                        name="Fan_02",
                        type_name="FanType",
                        include_optional=False,
                        values={"Speed": 900},
                    ),
                    parent=parent,
                )
                report_instance(lean)
                pump = await author.instantiate(
                    InstanceSpec(name="Pump_03", type_name="PumpType", values={"SetPoint": 61.0}),
                    parent=parent,
                )
                report_instance(pump)
                bullet(
                    f"{fan.browse_name} carries {len(fan.components)} parts, "
                    f"{lean.browse_name} carries {len(lean.components)}, "
                    f"from {len(fan.declared_names)} the type declares"
                )

                # ------------------------------------------------------------ 5
                heading("5. Reading the authored model back over a second, ordinary session")
                async with run.open_client() as quiet:
                    quiet_reader = AddressSpaceReader(quiet)
                    quiet_author = AddressSpaceAuthor(
                        quiet.get_node(ua.ObjectIds.ObjectsFolder), namespace_index=index
                    )
                    report_type(await quiet_reader.read_type(authored.node_id))
                    report_instance(await quiet_reader.describe_instance(fan.node_id))

                    # -------------------------------------------------------- 6
                    heading("6. What the contract says a client may change")
                    await attempt(
                        f"write {fan.browse_name}.Speed 2000 (declared Int32, read and write)",
                        author.write_component_value(fan.node_id, "Speed", 2000),
                    )
                    await attempt(
                        f"write {fan.browse_name}.Vendor (declared read only)",
                        author.write_component_value(fan.node_id, "Vendor", "vendor-a"),
                    )
                    await attempt(
                        f"write {lean.browse_name}.Speed from a session with no role",
                        quiet_author.write_component_value(lean.node_id, "Speed", 1750),
                    )
                    await attempt(
                        "write Vendor with the toolkit's check off, on a session with no role",
                        quiet_author.write_component_value(
                            fan.node_id, "Vendor", "vendor-b", enforce_access_level=False
                        ),
                    )
                    await attempt(
                        f"raw write_value(2000) on {fan.browse_name}.Speed",
                        quiet.get_node(ua.NodeId(f"{fan.browse_name}.Speed", index)).write_value(
                            2000
                        ),
                    )
                    await attempt(
                        f"raw write_value(61.0) on {pump.browse_name}.SetPoint, which is a Float",
                        quiet.get_node(
                            ua.NodeId(f"{pump.browse_name}.SetPoint", index)
                        ).write_value(61.0),
                    )
                    await attempt(
                        f"write {pump.browse_name}.Vibration 3.5 (declared Float, read only)",
                        author.write_component_value(pump.node_id, "Vibration", 3.5),
                    )

                    # -------------------------------------------------------- 7
                    heading("7. Two refusals this author makes before the server is asked")
                    await attempt(
                        "instantiate MachineType, which is abstract",
                        author.instantiate(
                            InstanceSpec(name="Machine_01", type_name="MachineType"), parent=parent
                        ),
                    )
                    await attempt(
                        "instantiate Fan_03 with a value for a part it would not have",
                        author.instantiate(
                            InstanceSpec(
                                name="Fan_03",
                                type_name="FanType",
                                include_optional=False,
                                values={"Vendor": "written anyway"},
                            ),
                            parent=parent,
                        ),
                    )
                    try:
                        half_written = await quiet_reader.describe_instance(
                            ua.NodeId("Fan_03", index)
                        )
                        bullet(f"Fan_03 exists in the address space: {half_written.summary_line()}")
                    except Exception as exc:
                        bullet(f"Fan_03 in the address space: {type(exc).__name__}")

                    # -------------------------------------------------------- 8
                    heading("8. The same AddNodes from a session the server does not trust")
                    await attempt(
                        "author a brand new type as an anonymous client",
                        quiet_author.author_object_type(
                            ObjectTypeSpec(
                                name="GhostType",
                                supertype="FanType",
                                components=(
                                    ComponentSpec(
                                        name="Level",
                                        data_type="Double",
                                        initial=1.0,
                                        rule=ModellingRule.MANDATORY,
                                    ),
                                ),
                            )
                        ),
                    )
                    await attempt(
                        "add an Object with asyncua's own helper, as an anonymous client",
                        quiet.get_node(ua.ObjectIds.ObjectsFolder).add_object(
                            ua.NodeId("GhostFolder", index), ua.QualifiedName("GhostFolder", index)
                        ),
                    )

                    # -------------------------------------------------------- 9
                    heading("9. Authoring the same model a second time")
                    for report in await author.author_model([spec]):
                        bullet(f"{report.browse_name} reused={report.reused}")
                        for note in report.notes:
                            bullet(f"   note: {note}")
                    again = await author.instantiate(
                        InstanceSpec(name="Fan_01", type_name="FanType"), parent=parent
                    )
                    for note in again.notes:
                        bullet(f"note: {note}")

                    # -------------------------------------------------------- 10
                    heading("10. An instance checked against a type it is not")
                    if pump_type_id is not None:
                        report_instance(
                            await quiet_reader.describe_instance(
                                lean.node_id, expected_type=pump_type_id
                            )
                        )

                    # -------------------------------------------------------- 11
                    heading("11. BrowsePath translation to a component of an authored instance")
                    if model_folder_id is not None:
                        quiet_explorer = NamespaceExplorer(quiet)
                        resolved = await quiet_explorer.resolve_path_with_translate(
                            [INSTANCE_NAME_FOR_PATH, COMPONENT_NAME_FOR_PATH],
                            root=model_folder_id,
                            namespace_index=index,
                        )
                        bullet(
                            f"{MODEL_FOLDER_NAME}/{INSTANCE_NAME_FOR_PATH}/"
                            f"{COMPONENT_NAME_FOR_PATH} -> {format_node_id(resolved.node_id)}"
                        )
                        await attempt(
                            f"translate {MODEL_FOLDER_NAME}/{INSTANCE_NAME_FOR_PATH}/Nope",
                            quiet_explorer.resolve_path_with_translate(
                                [INSTANCE_NAME_FOR_PATH, "Nope"], root=model_folder_id
                            ),
                        )

        # ---------------------------------------------------------------- 12
        heading("12. What the address space says about itself after the authoring")
        async with run.open_client() as client:
            reader = AddressSpaceReader(client)
            after = await show_coverage(reader, ua.ObjectIds.ObjectsFolder, "Objects folder")
            bullet(f"variables counted before / after: {before} -> {after}")
            bullet("next: Chapter 17 looks at what a value carries besides its number")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
