# coming soon!

# servertypes/servercapabilities/serveroperationallimits/serverstate/servicelevel/

# FIXME
# CHECK SERVERSTATE
# CHECK SERVICELEVEL
# EXPLORE SERVERCAPABILITIES
# TRY TO LOAD DATATYPEDEFINITIONS EXCEPT LOAD TYPEDEFINITIONS
# RESPECT OPERATIONALLIMITS: READ / WRITE / BROWSE / SUBSCRIBE

import asyncio
import logging
from typing import Union, List, Iterable
from asyncua import Client, ua, Node
from asyncua.common.subscription import Subscription

logging.basicConfig(level=logging.WARNING)
_logger = logging.getLogger('asyncua')

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
        print(f"{node} -> Datachange: {data.monitored_item.Value}")


async def subscribe(
    subscription: Subscription, 
    nodes: Union[Node, Iterable[Node]], 
    queuesize = 0,
    maxpercall: int = None, 
    attr = ua.AttributeIds.Value,
    monitoring = ua.MonitoringMode.Reporting
):
    '''
    Subscribing to large numbers of nodes without overwhelming the server!
    '''
    if not isinstance(nodes, list):
        nodes = [nodes]

    if not maxpercall:
        try:
            # try to read the "MaxMonitoredItemsPerCall" value
            maxpercall = await subscription.server.get_node("i=11714").read_value()
            if maxpercall == 0: 
                maxpercall = None
        except:
            maxpercall = None

    if not maxpercall:
        return await subscription.subscribe_data_change(nodes)
    else:
        handle_list = []
        for each in [nodes[i:i + maxpercall] for i in range(0, len(nodes), maxpercall)]:
            handles = await subscription.subscribe_data_change(
                nodes=each,
                attr=attr,
                queuesize=queuesize,
                monitoring=monitoring,
            )
            handle_list.append(handles)
        return handle_list

# TODO
async def unsubscribe():
    # reverse as in subscribe() same logic applies
    pass

async def main():
    client = Client(url="opc.tcp://127.0.0.1:48010", timeout=4)

    handler = SubscriptionHandler()
    await client.connect()

    print("-----------------------------------------------------")
    print("checking ServerState")
    # a complete list of alle ServerStates: https://reference.opcfoundation.org/v104/Core/DataTypes/ServerState/
    server_state = await client.get_node("ns=0;i=2259").read_value()
    print("ServerState:", server_state)
    if server_state is not 0:
        # if the ServerState is not 0 its better to disconnect and try again later
        await client.disconnect()
        return

    print("-----------------------------------------------------")
    print("checking ServiceLevel")
    # ServiceLevel ranges: https://reference.opcfoundation.org/v104/Core/docs/Part4/6.6.2/#Table109
    service_level = await client.get_node("ns=0;i=2267").read_value()
    print("ServiceLevel:", service_level)
    if service_level >= 200:
        print("The Server has a Healthy ServiceLevel")
    elif service_level >= 2 and service_level <= 199:
        print("The Server has a Degraded ServiceLevel")
        await client.disconnect()
        return
    elif service_level == 1:
        print("The Server has a NoData ServiceLevel")
        await client.disconnect()
        return
    elif service_level == 0:
        print("The Server has a Maintenance ServiceLevel")
        await client.disconnect()
        return
    else:
        raise ValueError(f"ServiceLevel: {service_level} is out of range byte (0-255)")

    print("-----------------------------------------------------")
    print("checking ServerCapabilities")
    server_capabilities_node = client.get_node("ns=0;i=2268") # ServerCapabilities

    print("checking OperationLimits")
    operational_limits = client.get_node("ns=0;i=11704") # OperationLimits
    operational_limits_children = await operational_limits.get_children()
    for child in operational_limits_children:
        name = await child.read_display_name()
        value = await child.read_value()
        print(f"{name.Text}: {value}")
    
    # typically at this point you need to adopt clientsettings to not make requests with to big nodelists
    # build your own read/write/browse methods which respect the "OperationLimits" and which take care of chunking and requesting

    print("-----------------------------------------------------")
    print("subscribe to a large number of nodes")
    nodes = await client.get_node("ns=2;s=Demo.Massfolder_Dynamic").get_variables()

    print(f"{len(nodes)} Nodes found!")

    subscription = await client.create_subscription(
                    period=5000,
                    handler=handler,
                    publishing=True
    )

    handles = await subscribe(subscription=subscription, nodes=nodes, queuesize=100, maxpercall=None)

    await asyncio.sleep(30)

    print("-----------------------------------------------------")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())