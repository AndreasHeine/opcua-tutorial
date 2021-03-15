import asyncio
from asyncua import Client, ua

async def main():
    client = Client(url="opc.tcp://127.0.0.1:4840", timeout=8)
    await client.connect()
    await client.load_data_type_definitions()
    
    print("-----------------------------------------------------")



    print("-----------------------------------------------------")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())