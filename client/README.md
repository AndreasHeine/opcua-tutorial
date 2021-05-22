The Server used in the Clients section is the "OPC UA C++ Demo Server V1.7.4"  
URL: "opc.tcp://127.0.0.1:48010"  

Best Practices:  
Clients should monitor its connection by reading cyclic the ServerState/ServiceLevel!  
Clients should Disconnect if ServerState or ServicLevel is not "0:Running" and >200!  
Clients should always check ServersCapabillities!  
Clients should always check OperationalLimits and adjust to them!  
Clients should Browse the TypeDefinition before writing it, if the DataType is not known!  
Clients should check the NamespaceArray for known Companion-Specifications!  
Clients should only access the Information they really need!
Clients should check the AccessLevel before writing to a Variable/Property!  
Clients should bundle the Read/Write/Browse requests where ever its possible to reduce the overhead per information!  
  
Please keep in mind most OPC UA Servers responde with an bad StatusCode if a Client violates the Servers Rules, to make your application more robust you should stick to the "Best Practices" so you have less garbage requests.  
  