import asyncio
import datetime
import logging
from asyncua import Client, ua, Node

logging.basicConfig(level=logging.WARNING)
_logger = logging.getLogger('asyncua')

'''
https://reference.opcfoundation.org/v104/Core/docs/Part4/5.13.1/
In simple words:
A OPC UA Subscription works like a "Mailbox" which gets emptied in a defined interval.
The OPC UA Client sends a publishrequest, the server takes all notifications since the last publishrequest
and send them in the publishresponse to the client.
The OPC UA Client can add "MonitoredItems" to the Subscription which will generate Notifications.
'''

class SubscriptionHandler:
    """
    The SubscriptionHandler is used to handle the data that is received for the subscription.
    """
    async def datachange_notification(self, node: Node, val, data):
        """
        Callback for asyncua Subscription.
        This method will be called when the Client received a data change message from the Server.
        """
        print(f"Datachange: {data.monitored_item.Value}")

async def main():
    client = Client(url="opc.tcp://127.0.0.1:48010", timeout=4)
    handler = SubscriptionHandler() # get an instance of the SubscriptionHandler-Class
    await client.connect()

    print("-----------------------------------------------------")

    nodes = await client.get_node("ns=2;s=Demo.Dynamic.Scalar").get_children() # just get some nodes to subscribe
    subscription = await client.create_subscription(
                    period=1000, # the client will send each 1000 ms a publishrequest and the server responds with the changes since last publishrequest
                    handler=handler, # SubscriptionHandler which will be used for processing the notifications in the publishresponse
                    publishing=True
                )
    # Reporting            
    node_handles = await subscription.subscribe_data_change(
        nodes=nodes, # a list of nodes i want to subscribe to
        attr=ua.AttributeIds.Value, # the attribute i am interested in
        queuesize=50, # the queuesize should be bigger then the number of changes within a publishinterval, in this case 50 valuechanges per 1000 ms
        monitoring=ua.MonitoringMode.Reporting
    )

    # Sampling
    # TODO

    await asyncio.sleep(10)

    # Modify
    # TODO

    await asyncio.sleep(10)
    await subscription.unsubscribe(node_handles)

    print("-----------------------------------------------------")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())