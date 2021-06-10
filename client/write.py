# coming soon!

#variant/datavalue/typesafe-write/

import asyncio
import datetime
import logging
from asyncua import Client, ua

logging.basicConfig(level=logging.WARNING)
_logger = logging.getLogger('asyncua')

async def main():
    client = Client(url="opc.tcp://127.0.0.1:48010", timeout=8)
    await client.connect()

    print("-----------------------------------------------------")

    double_node = client.get_node("ns=2;s=Demo.Static.Scalar.Double")
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
    print("Write to the node:")
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

    print("-----------------------------------------------------")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())