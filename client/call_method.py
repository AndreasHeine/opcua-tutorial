import asyncio
import logging
from asyncua import Client, ua

logging.basicConfig(level=logging.WARNING)
_logger = logging.getLogger('asyncua')

async def main():
    client = Client(url="opc.tcp://127.0.0.1:48010", timeout=8)
    await client.connect()
    
    method_parent = client.get_node("ns=7;s=Demo.CTT.Methods")
    method_node = client.get_node("ns=7;s=Demo.CTT.Methods.MethodIO") # the method just adds two Int32 numbers

    # always specify the correct VariantType! 
    inarg1 = ua.Variant(20, ua.VariantType.UInt32) 
    inarg2 = ua.Variant(13, ua.VariantType.UInt32)

    result = await method_parent.call_method(method_node, inarg1, inarg2) # the order of args matters!
    print(result)

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
