@description('Name of the user-assigned identity used by AKS pods')
param name string

param location string = resourceGroup().location
param tags object = {}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: name
  location: location
  tags: tags
}

output id string = identity.id
output name string = identity.name
output clientId string = identity.properties.clientId
output principalId string = identity.properties.principalId