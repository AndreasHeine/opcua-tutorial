import asyncio
import logging
from asyncua import Client, ua

logging.basicConfig(level=logging.WARNING)
_logger = logging.getLogger('asyncua')

async def main():
    client = Client(url="opc.tcp://127.0.0.1:48010", timeout=8)
    await client.connect()

    print("-----------------------------------------------------")
    # get the references of a single node
    obj = client.get_objects_node()
    refs = await obj.get_references()
    children = []
    type_definitions = []
    print(f"Node-Id {obj} has following References:")
    for ref in refs:
        print(ref)
        if ref.ReferenceTypeId.Identifier == ua.ObjectIds.Organizes:
            # Organizes = 35
            children.append(ref)
        elif ref.ReferenceTypeId.Identifier == ua.ObjectIds.HasTypeDefinition:
            # HasTypeDefinition = 40
            type_definitions.append(ref)
        else:
            pass
    print("-----------------------------------------------------")
    print(f"Node-Id {obj} has following Childnodes:")
    for child in children:
        print(child.BrowseName.Name)
    print("-----------------------------------------------------")
    print(f"Node-Id {obj} is from Type:")
    for type_definition in type_definitions:
        print(type_definition.BrowseName.Name)

    print("-----------------------------------------------------")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
