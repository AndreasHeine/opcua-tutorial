import asyncio
import logging
from asyncua import Client, ua

logging.basicConfig(level=logging.WARNING)
_logger = logging.getLogger('asyncua')

async def main():
    client = Client(url="opc.tcp://127.0.0.1:48010", timeout=4)
    # client.application_uri = "..."
    # client.session_timeout = 30000 # requested value will be negotiated between client and server
    # client.description = "..."
    # client.product_uri = "..."
    # client.name = "..."
    # client.max_chunkcount = 0 # requested value will be negotiated between client and server
    # client.max_messagesize = 0 # requested value will be negotiated between client and server
    # client.set_user("user")
    # client.set_password("pw")
    # await client.set_security_string("Basic256Sha256,SignAndEncrypt,cert.pem,key.pem")

    # catching all networkrequests is some times important
    try:
        await client.connect()
    except Exception as e: # catching all exceptions is not always wise!
        print(e)
        return

    '''
    clientcode!

    '''

    try:
        await client.disconnect()
    except Exception as e:  # catching all exceptions is not always wise!
        print(e)
        return


if __name__ == "__main__":
    asyncio.run(main())
