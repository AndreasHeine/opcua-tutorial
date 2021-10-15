### The Server used in the Clients section is the "OPC UA C++ Demo Server V1.7.4 (Windows)" (https://www.unified-automation.com/de/downloads/opc-ua-servers.html)    

# Best Practices:  
:exclamation: Clients should monitor its connection by reading cyclic the ServerState/ServiceLevel!  
:exclamation: Clients should Disconnect if ServerState or ServicLevel is not "0:Running" and <200!  
:exclamation: Clients should always check ServersCapabillities!  
:exclamation: Clients should always check OperationalLimits and adjust to them!  
:exclamation: Clients should Browse the TypeDefinition before writing it, if the DataType is not known!  
:exclamation: Clients should check the NamespaceArray for known Companion-Specifications!  
:exclamation: Clients should only access the Information they really need!  
:exclamation: Clients should check the AccessLevel before writing to a Variable/Property!  
:exclamation: Clients should bundle the Read/Write/Browse requests where ever its possible to reduce the overhead per information!  
  
## Please keep in mind most OPC UA Servers responde with an bad statuscode if a client violates the servers rules, to make your application more robust you should stick to the "Best Practices" so you have less garbage requests.  
  
