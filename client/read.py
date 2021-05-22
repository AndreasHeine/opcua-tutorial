import asyncio
import logging
from asyncua import Client, ua

logging.basicConfig(level=logging.WARNING)
_logger = logging.getLogger('asyncua')

async def main():
    client = Client(url="opc.tcp://127.0.0.1:48010", timeout=8)
    await client.connect()

    print("-----------------------------------------------------")
    # read one node attribute at a time
    node = client.get_node("ns=0;i=2267") # ServiceLevel
    print("Value: ", await node.read_value())
    print("DataValue: ", await node.read_data_value())
    print("BrowseName: ", await node.read_browse_name())
    print("DataType: ", await node.read_data_type())
    print("DisplayName: ", await node.read_display_name())

    print("-----------------------------------------------------")
    # read multiple node attributes at once
    print("ReadValues:")
    result = await client.read_values([
        client.get_node("ns=0;i=2267"), 
        client.get_node("ns=0;i=2256")
        # ...
    ])
    for res in result:
        print(res)

    print("-----------------------------------------------------")  
    print("ReadAttributeValues:")
    result = await client.uaclient.read_attributes([
        client.get_node("ns=0;i=2267").nodeid, 
        client.get_node("ns=0;i=2256").nodeid
        # ...
        ], 
        ua.AttributeIds.Value
    )
    for res in result:
        print(res)

    print("-----------------------------------------------------")
    print("ReadBrowseNames:")
    result = await client.uaclient.read_attributes([
        client.get_node("ns=0;i=2267").nodeid, 
        client.get_node("ns=0;i=2256").nodeid
        # ...
        ], 
        ua.AttributeIds.BrowseName
    )
    for res in result:
        print(res)

    print("-----------------------------------------------------")
    print("ReadDataTypes:")
    result = await client.uaclient.read_attributes([
        client.get_node("ns=0;i=2267").nodeid, 
        client.get_node("ns=0;i=2256").nodeid
        # ...
        ], 
        ua.AttributeIds.DataType
    )
    for res in result:
        print(res)

    print("-----------------------------------------------------")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())