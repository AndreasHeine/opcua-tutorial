import asyncio
import datetime
import logging
from asyncua import Client, ua

logging.basicConfig(level=logging.WARNING)
_logger = logging.getLogger('asyncua')

def check_writeable_bit(value):
    '''
    https://reference.opcfoundation.org/v104/Core/DataTypes/AccessLevelExType/

    '''
    if value & 0b10 > 0: # check if bit1 (CurrentWrite) is set
        return True
    return False

async def main():
    client = Client(url="opc.tcp://127.0.0.1:48010", timeout=4)
    await client.connect()

    print("-----------------------------------------------------")

    double_node = client.get_node("ns=2;s=Demo.Static.Scalar.Double") # returns a Node-Class

    # check if Variable is writeable
    res = await double_node.read_attribute(ua.AttributeIds.AccessLevel) # returns DataValue-Class
    accesslevel = res.Value.Value
    print("AccessLevel:", accesslevel)

    if check_writeable_bit(accesslevel):
        print("The Variable is writeable!")
        print("-----------------------------------------------------")

        # always specify the correct VariantType!
        dv = ua.DataValue(ua.Variant(200.0, ua.VariantType.Double))
        print("Write to the node:")
        print(dv)
        await double_node.write_value(dv)
        res = await double_node.read_data_value()
        print("Read the node:")
        print(res)

        print("-----------------------------------------------------")

        dv = ua.DataValue(ua.Variant(100.0, ua.VariantType.Double))
        print("Write to a node by specify the attribute:")
        print(dv)
        await client.uaclient.write_attributes(
            nodeids= [
                double_node.nodeid,
            ],
            datavalues= [
                dv,
            ],
            attributeid= ua.AttributeIds.Value
        )

        res = await double_node.read_data_value()
        print("Read the node:")
        print(res)
    else:
        print("The Variable is not writeable!")

    print("-----------------------------------------------------")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())