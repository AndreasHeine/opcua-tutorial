# coming soon!

# servertypes/servercapabilities/serveroperationallimits/serverstate/servicelevel/

# FIXME
# CHECK SERVERSTATE
# CHECK SERVICELEVEL
# EXPLORE SERVERCAPABILITIES
# TRY TO LOAD DATATYPEDEFINITIONS EXCEPT LOAD TYPEDEFINITIONS
# RESPECT OPERATIONALLIMITS: READ / WRITE / BROWSE / SUBSCRIBE
'''
    maxpercall = await client.get_node("i=11714").read_value() # 1000 nodes
    chunks = int(len(nodes)/maxpercall)
    chunks += 1
    print(chunks)
    nodes_to_sub = []
    if chunks == 1:
        nodes_to_sub = nodes
        await subscription.subscribe_data_change(nodes_to_sub)
    if chunks > 1:
        i = 0
        while 1:
            i+=1
            nodes_to_sub = nodes[:maxpercall]
            print(i, "Subscribing to:", len(nodes_to_sub), "Nodes")
            await subscription.subscribe_data_change(nodes_to_sub)
            del nodes[:maxpercall]
            if len(nodes) <= 0:
                break
'''