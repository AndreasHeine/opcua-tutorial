import asyncio
import logging
from asyncua import Client, ua

logging.basicConfig(level=logging.WARNING)
_logger = logging.getLogger('asyncua')

async def main():
    client = Client(url="opc.tcp://127.0.0.1:48010", timeout=4)
    await client.connect()

    print("-----------------------------------------------------")
    # read one node attribute at a time
    node = client.get_node("ns=0;i=2267") # ServiceLevel
    print("Value: ", await node.read_value()) # returns just the Value
    print("DataValue: ", await node.read_data_value()) # returns the DataValue (Value + StatusCode + Timestamps)
    print("BrowseName: ", await node.read_browse_name()) # returns the BrowseName ("0:ServiceLevel")
    print("DataType: ", await node.read_data_type()) # returns the DataType of the Value (i=3 -> Byte)
    print("DisplayName: ", await node.read_display_name()) # returns the DisplayName in form of LocalizedText

    print("-----------------------------------------------------")
    # read multiple node attributes at once (HighLevel)
    print("ReadValues:")
    result = await client.read_values([
        client.get_node("ns=0;i=2267"), # Node-Class
        client.get_node("ns=0;i=2256")  # Node-Class
        # ...
    ])
    for res in result:
        print(res)

    print("-----------------------------------------------------")  
    print("ReadAttributeValues:")
    result = await client.uaclient.read_attributes([
        ua.NodeId.from_string("ns=0;i=2267"), # NodeId-Class
        ua.NodeId.from_string("ns=0;i=2256")  # NodeId-Class
        # ...
        ], 
        ua.AttributeIds.Value
    )
    # read_attributes returns a list of DataValue-Class instances (Value + StatusCode + Timestamps) which represent the Values
    for res in result:
        print(res)

    print("-----------------------------------------------------")
    print("ReadBrowseNames:")
    result = await client.uaclient.read_attributes([
        ua.NodeId.from_string("ns=0;i=2267"), # NodeId-Class
        ua.NodeId.from_string("ns=0;i=2256")  # NodeId-Class
        # ...
        ], 
        ua.AttributeIds.BrowseName
    )
    # read_attributes returns a list of DataValue-Class instances (Value + StatusCode + Timestamps) which represent the BrowseNames
    for res in result:
        print(res)

    print("-----------------------------------------------------")
    print("ReadDataTypes:")
    result = await client.uaclient.read_attributes([
        ua.NodeId.from_string("ns=0;i=2267"), # NodeId-Class
        ua.NodeId.from_string("ns=0;i=2256")  # NodeId-Class
        # ...
        ], 
        ua.AttributeIds.DataType
    )
    # read_attributes returns a list of DataValue-Class instances (Value + StatusCode + Timestamps) which represent the DataTypes
    for res in result:
        print(res)

    print("-----------------------------------------------------")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())