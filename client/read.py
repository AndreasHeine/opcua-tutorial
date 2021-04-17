import asyncio
from asyncua import Client, ua

async def main():
    client = Client(url="opc.tcp://127.0.0.1:4840/UA", timeout=8)
    client.set_user("user")
    client.set_password("pw")
    await client.connect()
    # await client.load_data_type_definitions() # v1.04
    await client.load_type_definitions() # v1.03

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
    result = await client.read_values([client.get_node("ns=0;i=2267"), client.get_node("ns=0;i=2256")])
    for res in result:
        print(res)

    print("-----------------------------------------------------")  
    print("ReadAttributeValues:")
    result = await client.uaclient.read_attributes([client.get_node("ns=0;i=2267").nodeid, client.get_node("ns=0;i=2256").nodeid], ua.AttributeIds.Value)
    for res in result:
        print(res)

    print("-----------------------------------------------------")
    print("ReadBrowseNames:")
    result = await client.uaclient.read_attributes([client.get_node("ns=0;i=2267").nodeid, client.get_node("ns=0;i=2256").nodeid], ua.AttributeIds.BrowseName)
    for res in result:
        print(res)

    print("-----------------------------------------------------")
    print("ReadDataTypes:")
    result = await client.uaclient.read_attributes([client.get_node("ns=0;i=2267").nodeid, client.get_node("ns=0;i=2256").nodeid], ua.AttributeIds.DataType)
    for res in result:
        print(res)

    print("-----------------------------------------------------")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())