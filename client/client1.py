# coming soon!

# servertypes/servercapabilities/serveroperationallimits/serverstate/servicelevel/

# FIXME
# CHECK SERVERSTATE
# CHECK SERVICELEVEL
# EXPLORE SERVERCAPABILITIES
# TRY TO LOAD DATATYPEDEFINITIONS EXCEPT LOAD TYPEDEFINITIONS
# RESPECT OPERATIONALLIMITS: READ / WRITE / BROWSE / SUBSCRIBE

import asyncio
from asyncua import Client, ua, Node
from asyncua.common.subscription import Subscription

async def subscribe(
    subscription: Subscription, 
    nodes: Union[Node, Iterable[Node]], 
    maxpercall: int = None, 
    attr = ua.AttributeIds.Value,
    queuesize = 0,
    monitoring = ua.MonitoringMode.Reporting
):
    '''
    Subscribing to large numbers of nodes without overwhelming the server!
    '''
    if not maxpercall:
        try:
            maxpercall = await subscription.server.get_node("i=11714").read_value()
        except:
            maxpercall = 0
    if maxpercall == 0:
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
        return handles

# TODO
async def unsubscribe():
    pass