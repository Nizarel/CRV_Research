@description('Name of the dedicated Application Gateway managed by AGIC')
param name string

param location string = resourceGroup().location
param tags object = {}

@description('Dedicated Application Gateway subnet resource ID')
param subnetId string

@description('Name of the public IP address assigned to the gateway')
param publicIpName string

@minValue(0)
@maxValue(125)
@description('Minimum WAF_v2 capacity')
param minCapacity int = 1

@minValue(2)
@maxValue(125)
@description('Maximum WAF_v2 capacity')
param maxCapacity int = 2

@description('Principal ID used by AGIC to reconcile Application Gateway configuration')
param agicPrincipalId string

resource publicIp 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: publicIpName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Regional'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource wafPolicy 'Microsoft.Network/ApplicationGatewayWebApplicationFirewallPolicies@2024-05-01' = {
  name: '${name}-waf'
  location: location
  tags: tags
  properties: {
    policySettings: {
      state: 'Enabled'
      mode: 'Prevention'
      requestBodyCheck: true
      maxRequestBodySizeInKb: 128
      fileUploadLimitInMb: 100
    }
    managedRules: {
      managedRuleSets: [
        {
          ruleSetType: 'OWASP'
          ruleSetVersion: '3.2'
        }
      ]
    }
  }
}

// The telemetry relay accepts opaque Application Insights envelopes that can match OWASP signatures.
// Detection mode is scoped only to the exact relay path by its dedicated Ingress.
resource telemetryWafPolicy 'Microsoft.Network/ApplicationGatewayWebApplicationFirewallPolicies@2024-05-01' = {
  name: '${name}-telemetry-waf'
  location: location
  tags: tags
  properties: {
    policySettings: {
      state: 'Enabled'
      mode: 'Detection'
      requestBodyCheck: true
      maxRequestBodySizeInKb: 128
      fileUploadLimitInMb: 100
    }
    managedRules: {
      managedRuleSets: [
        {
          ruleSetType: 'OWASP'
          ruleSetVersion: '3.2'
        }
      ]
    }
  }
}

resource telemetryWafPolicyAgicContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(telemetryWafPolicy.id, agicPrincipalId, 'b24988ac-6180-42a0-ab88-20f7382dd24c')
  scope: telemetryWafPolicy
  properties: {
    principalId: agicPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b24988ac-6180-42a0-ab88-20f7382dd24c') // Contributor
  }
}

resource gateway 'Microsoft.Network/applicationGateways@2024-05-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'WAF_v2'
      tier: 'WAF_v2'
    }
    autoscaleConfiguration: {
      minCapacity: minCapacity
      maxCapacity: maxCapacity
    }
    gatewayIPConfigurations: [
      {
        name: 'gateway-ip-configuration'
        properties: {
          subnet: {
            id: subnetId
          }
        }
      }
    ]
    frontendIPConfigurations: [
      {
        name: 'public-frontend'
        properties: {
          publicIPAddress: {
            id: publicIp.id
          }
        }
      }
    ]
    frontendPorts: [
      {
        name: 'http'
        properties: {
          port: 80
        }
      }
    ]
    backendAddressPools: [
      {
        name: 'agic-bootstrap-pool'
        properties: {
          backendAddresses: []
        }
      }
    ]
    backendHttpSettingsCollection: [
      {
        name: 'agic-bootstrap-http-settings'
        properties: {
          port: 80
          protocol: 'Http'
          cookieBasedAffinity: 'Disabled'
          requestTimeout: 20
        }
      }
    ]
    httpListeners: [
      {
        name: 'agic-bootstrap-listener'
        properties: {
          frontendIPConfiguration: {
            id: resourceId('Microsoft.Network/applicationGateways/frontendIPConfigurations', name, 'public-frontend')
          }
          frontendPort: {
            id: resourceId('Microsoft.Network/applicationGateways/frontendPorts', name, 'http')
          }
          protocol: 'Http'
        }
      }
    ]
    requestRoutingRules: [
      {
        name: 'agic-bootstrap-rule'
        properties: {
          ruleType: 'Basic'
          priority: 1000
          httpListener: {
            id: resourceId('Microsoft.Network/applicationGateways/httpListeners', name, 'agic-bootstrap-listener')
          }
          backendAddressPool: {
            id: resourceId('Microsoft.Network/applicationGateways/backendAddressPools', name, 'agic-bootstrap-pool')
          }
          backendHttpSettings: {
            id: resourceId('Microsoft.Network/applicationGateways/backendHttpSettingsCollection', name, 'agic-bootstrap-http-settings')
          }
        }
      }
    ]
    enableHttp2: true
    firewallPolicy: {
      id: wafPolicy.id
    }
  }
}

resource agicGatewayContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(gateway.id, agicPrincipalId, 'b24988ac-6180-42a0-ab88-20f7382dd24c')
  scope: gateway
  properties: {
    principalId: agicPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b24988ac-6180-42a0-ab88-20f7382dd24c') // Contributor
  }
}

resource gatewaySubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  name: '${split(subnetId, '/')[8]}/${split(subnetId, '/')[10]}'
}

resource agicSubnetNetworkContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(gatewaySubnet.id, agicPrincipalId, '4d97b98b-1d4f-4787-a291-c67834d212e7')
  scope: gatewaySubnet
  properties: {
    principalId: agicPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4d97b98b-1d4f-4787-a291-c67834d212e7') // Network Contributor
  }
}

output id string = gateway.id
output name string = gateway.name
output publicIpAddress string = publicIp.properties.ipAddress
output publicIpResourceId string = publicIp.id
