import asyncio
import logging
from asyncua import Client, ua

logging.basicConfig(level=logging.WARNING)
_logger = logging.getLogger('asyncua')

async def main():
    client = Client(url="opc.tcp://127.0.0.1:48010", timeout=8)
    await client.connect()
    obj = client.get_objects_node()

    print("-----------------------------------------------------")
    # get the references of a single node
    refs = await obj.get_references()
    organizes = []
    typedefinitions = []
    print(f"Node-Id {obj} has following References:")
    for ref in refs:
        print(ref)
        if ref.ReferenceTypeId.Identifier == ua.ObjectIds.Organizes:
            # Organizes = 35
            organizes.append(ref)
        elif ref.ReferenceTypeId.Identifier == ua.ObjectIds.HasTypeDefinition:
            # HasTypeDefinition = 40
            typedefinitions.append(ref)
        else:
            pass
    print("-----------------------------------------------------")
    print(f"Node-Id {obj} has following Childnodes:")
    for each in organizes:
        print(each.BrowseName.Name)
    print("-----------------------------------------------------")
    print(f"Node-Id {obj} is from Type:")
    for each in typedefinitions:
        print(each.BrowseName.Name)

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
