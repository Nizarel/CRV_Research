@description('Name of the AKS cluster')
param name string

param location string = resourceGroup().location
param tags object = {}

@description('Subnet used by AKS nodes')
param subnetId string

@description('Log Analytics workspace resource ID for Container Insights')
param logAnalyticsWorkspaceResourceId string = ''

@description('Whether this deployment manages the AKS Container Insights addon (omsagent). Disable temporarily to bypass conflicting extension operations.')
param enableMonitoringAddon bool = true

@description('Enable the Key Vault Secrets Store CSI provider')
param enableKeyVaultSecretsProvider bool = true

@description('Key Vault secret rotation polling interval')
param keyVaultRotationPollInterval string = '5m'

@description('Virtual machine size for the system and application nodes')
param nodeVmSize string = 'Standard_D4ds_v5'

@description('Minimum node count for the system pool')
param minNodeCount int = 2

@description('Maximum node count for the system pool')
param maxNodeCount int = 5

@retryOn([
  'InternalOperationError'
  'ServiceUnavailable'
], 8)
resource cluster 'Microsoft.ContainerService/managedClusters@2024-09-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'Base'
    tier: 'Standard'
  }
  properties: {
    dnsPrefix: name
    enableRBAC: true
    agentPoolProfiles: [
      {
        name: 'system'
        mode: 'System'
        count: minNodeCount
        minCount: minNodeCount
        maxCount: maxNodeCount
        enableAutoScaling: true
        vmSize: nodeVmSize
        osSKU: 'AzureLinux'
        osDiskType: 'Ephemeral'
        type: 'VirtualMachineScaleSets'
        vnetSubnetID: subnetId
      }
    ]
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      networkDataplane: 'cilium'
      networkPolicy: 'cilium'
      outboundType: 'loadBalancer'
      loadBalancerProfile: {
        managedOutboundIPs: {
          count: 1
        }
      }
      podCidr: '10.244.0.0/16'
      serviceCidr: '10.1.0.0/16'
      dnsServiceIP: '10.1.0.10'
    }
    apiServerAccessProfile: {
      enablePrivateCluster: true
      enablePrivateClusterPublicFQDN: false
    }
    oidcIssuerProfile: {
      enabled: true
    }
    securityProfile: {
      workloadIdentity: {
        enabled: true
      }
    }
    workloadAutoScalerProfile: {
      keda: {
        enabled: true
      }
    }
    autoUpgradeProfile: {
      upgradeChannel: 'patch'
      nodeOSUpgradeChannel: 'NodeImage'
    }
    addonProfiles: union(
      (enableMonitoringAddon && !empty(logAnalyticsWorkspaceResourceId)) ? {
        omsagent: {
          enabled: true
          config: {
            logAnalyticsWorkspaceResourceID: logAnalyticsWorkspaceResourceId
          }
        }
      } : {},
      enableKeyVaultSecretsProvider ? {
        azureKeyvaultSecretsProvider: {
          enabled: true
          config: {
            enableSecretRotation: 'true'
            rotationPollInterval: keyVaultRotationPollInterval
          }
        }
      } : {}
    )
  }
}

output id string = cluster.id
output name string = cluster.name
output kubeletPrincipalId string = cluster.properties.identityProfile.kubeletidentity.objectId
output oidcIssuerUrl string = cluster.properties.oidcIssuerProfile.issuerURL
output principalId string = cluster.identity.principalId