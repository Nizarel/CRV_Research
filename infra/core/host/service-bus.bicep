metadata description = 'Creates an Azure Service Bus namespace with a work queue and dead-letter settings for the ingestion pipeline.'

@description('Name of the Service Bus namespace.')
param name string

@description('Location for the namespace.')
param location string = resourceGroup().location

@description('Tags to apply to the namespace.')
param tags object = {}

@allowed(['Basic', 'Standard', 'Premium'])
@description('Service Bus SKU. Sessions and larger messages require Standard or Premium.')
param skuName string = 'Standard'

@description('Name of the ingestion work queue.')
param queueName string = 'ingestion-work'

@description('Max delivery attempts before a message is dead-lettered.')
param maxDeliveryCount int = 5

@allowed(['Enabled', 'Disabled'])
@description('Public network access. Set to Disabled when using private endpoints.')
param publicNetworkAccess string = 'Enabled'

resource namespace 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuName
  }
  properties: {
    disableLocalAuth: true
    publicNetworkAccess: publicNetworkAccess
    minimumTlsVersion: '1.2'
  }

  resource queue 'queues@2022-10-01-preview' = {
    name: queueName
    properties: {
      // Native dead-letter queue; failures beyond maxDeliveryCount are moved to the DLQ.
      deadLetteringOnMessageExpiration: true
      maxDeliveryCount: maxDeliveryCount
      lockDuration: 'PT5M'
      requiresSession: false
      enablePartitioning: false
    }
  }
}

output id string = namespace.id
output name string = namespace.name
output queueName string = namespace::queue.name
output serviceBusEndpoint string = namespace.properties.serviceBusEndpoint
