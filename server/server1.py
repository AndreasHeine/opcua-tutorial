import asyncio
import logging
from datetime import datetime
from asyncua import Server, ua

logging.basicConfig(level=logging.DEBUG)
_logger = logging.getLogger('asyncua')

async def main():
     # Serversetup
    server = Server()
    server.name = "OPC UA Tutorial Server"
    await server.init()
    await server.set_build_info(
        product_uri="https://github.com/andreasheine",
        product_name="Tutorial Server by Andreas Heine",
        manufacturer_name="Andreas Heine",
        software_version="beta",
        build_number="---",
        build_date=datetime.utcnow(),
    )
    server.set_endpoint("opc.tcp://0.0.0.0:4840")

    async with server:
        while 1:
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())