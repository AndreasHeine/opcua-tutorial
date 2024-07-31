import asyncio
import datetime
import logging
from asyncua import Client, ua, Node

logging.basicConfig(level=logging.WARNING)
_logger = logging.getLogger('asyncua')

'''
https://reference.opcfoundation.org/v104/Core/docs/Part4/5.13.1/
In simple words:
A OPC UA Subscription works like a "Mailbox" which gets emptied in a defined interval by the Client.
The OPC UA Client sends a publishrequest, the server takes all notifications since the last publishrequest.
and send them in the publishresponse to the client.
If there is no Notification in the "Mailbox", after a while the Client will get a "keep-alive" back and then the client will send another publishrequest.
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
        # DO NOT DO BLOCKING TASKS HERE , IF YOU NEED TO PROCESS THE DATA PUT IT ON A QUEUE AND PUT IT IN A SUBPROCESS (E.G. SQL QUERYS)!
        print(f"Datachange: {data.monitored_item.Value}")


async def main():
    client = Client(url="opc.tcp://127.0.0.1:48010", timeout=4)
    handler = SubscriptionHandler() # get an instance of the SubscriptionHandler-Class
    await client.connect()

    print("-----------------------------------------------------")

    nodes = await client.get_node("ns=2;s=Demo.Dynamic.Scalar").get_children() # just get some nodes to subscribe
    nodes.pop(8) # remove the gif (long bytestring) it would dump the console ;)
    print("NodesToSubscribe", nodes)

    # Create a Subscription:
    subscription = await client.create_subscription(
                    period=1000, # the client will send each 1000 ms a publishrequest and the server responds with the changes since last publishrequest
                    handler=handler, # SubscriptionHandler which will be used for processing the notifications in the publishresponse
                    publishing=True
    )
    print("Created Subscription with Id:", subscription.subscription_id)

    print("Start Reporting:")

    # Reporting:
    # each Attribute change will generate a Notification  
    node_handles = await subscription.subscribe_data_change(
        nodes=nodes, # a list of nodes i want to subscribe to
        attr=ua.AttributeIds.Value, # the attribute i am interested in
        queuesize=50, # the queuesize should be bigger then the number of changes within a publishinterval, in this case 50 valuechanges per 1000 ms
        monitoring=ua.MonitoringMode.Reporting,
        sampling_interval=250 # -1: Inherit from PublishInterval / 0: Eventbased / x: requested samping time (might be revised by the server)
    )

    # manually setting a filter (default in OPC UA Spec. is StatusValue)
    # await subscription._subscribe(
    #     nodes,
    #     ua.AttributeIds.Value,
    #     ua.DataChangeFilter(ua.DataChangeTrigger.StatusValueTimestamp),
    #     50,
    #     ua.MonitoringMode.Reporting,
    #     250
    # )

    await asyncio.sleep(5)
    await subscription.unsubscribe(node_handles)

    print("-----------------------------------------------------")

    print("Start Sampling:")
    print("Selected node: ns=2;s=Demo.Dynamic.Scalar.Float")

    node_handles = await subscription.subscribe_data_change(
    nodes=[client.get_node("ns=2;s=Demo.Dynamic.Scalar.Float")], # 
    attr=ua.AttributeIds.Value, # the attribute i am interested in
    queuesize=50, # the queuesize should be bigger then the number of changes within a publishinterval, in this case 50 valuechanges per 1000 ms
    monitoring=ua.MonitoringMode.Sampling
    )
    print("The OPC UA Server is now sampling in the background, till we change MonitoringMode to Reporting!")
    print("-----------------------------------------------------")

    await asyncio.sleep(10)
    print("Changing MonitoringMode to Reporting, now we should recv all queued/sampled Notifications!")
    await subscription.set_monitoring_mode(ua.MonitoringMode.Reporting)


    await asyncio.sleep(2)
    await subscription.unsubscribe(node_handles)

    print("-----------------------------------------------------")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
