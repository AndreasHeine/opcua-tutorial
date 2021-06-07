import asyncio
import logging
from asyncua import Client, ua
from asyncua.common.methods import call_method

logging.basicConfig(level=logging.WARNING)
_logger = logging.getLogger('asyncua')

async def main():
    client = Client(url="opc.tcp://127.0.0.1:48010", timeout=8)
    await client.connect()
    
    print("-----------------------------------------------------")

    method_parent = client.get_node("ns=7;s=Demo.CTT.Methods")
    method_node = client.get_node("ns=7;s=Demo.CTT.Methods.MethodIO") # the method just adds two Int32 numbers
    method_inputs = client.get_node("ns=7;s=Demo.CTT.Methods.MethodIO.InputArguments")
    method_ouputs = client.get_node("ns=7;s=Demo.CTT.Methods.MethodIO.OutputArguments")

    # if you dont know the Input-/OutputArguments:
    inputs = await method_inputs.read_value() # returns a list of Argument-Class
    print("Inputs:")
    for each in inputs:
        dtype = await client.get_node(each.DataType).read_display_name()
        print("Name:", each.Name, "DataType:", each.DataType, "->", dtype.Text)

    print("-----------------------------------------------------")

    outputs = await method_ouputs.read_value() # returns a list of Argument-Class
    print("Outputs:")
    for each in outputs:
        dtype = await client.get_node(each.DataType).read_display_name()
        print("Name:", each.Name, "DataType:", each.DataType, "->", dtype.Text)

    print("-----------------------------------------------------")

    # sometimes its important to check if a method is executable/userexecutable:
    executable = await client.uaclient.read_attributes([method_node.nodeid], ua.AttributeIds.Executable)
    print("Executable:", executable[0].Value.Value)
    userexecutable = await client.uaclient.read_attributes([method_node.nodeid], ua.AttributeIds.UserExecutable)
    print("Userxecutable:", userexecutable[0].Value.Value)

    print("-----------------------------------------------------")

    # always specify the correct VariantType! 
    inarg1 = ua.Variant(20, ua.VariantType.UInt32)
    inarg2 = ua.Variant(13, ua.VariantType.UInt32)

    if executable[0].Value.Value and userexecutable[0].Value.Value:
        print(f"Calling Method: {inarg1.Value} + {inarg2.Value}")
        # calling a method from Node-Class (parentnode)
        result = await method_parent.call_method(method_node, inarg1, inarg2) # the order of args matters!
        print("Method Result:", result)

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
