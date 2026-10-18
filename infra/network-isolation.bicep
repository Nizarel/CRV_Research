metadata description = 'Creates the private AKS network, private endpoint DNS dependencies, and a dedicated Application Gateway subnet.'

@description('The name of the VNet to create')
param vnetName string

param location string = resourceGroup().location
param tags object = {}
param useVpnGateway bool = false
param vpnGatewayName string = '${vnetName}-vpn-gateway'
param dnsResolverName string = '${vnetName}-dns-resolver'

var privateEndpointsSubnetName = 'private-endpoints-subnet'
var applicationGatewaySubnetName = 'application-gateway-subnet'
var aksSubnetName = 'aks-subnet'
var gatewaySubnetName = 'GatewaySubnet'
var dnsResolverSubnetName = 'dns-resolver-subnet'

module privateEndpointsNSG 'br/public:avm/res/network/network-security-group:0.5.1' = {
  name: 'private-endpoints-nsg'
  params: {
    name: '${vnetName}-private-endpoints-nsg'
    location: location
    tags: tags
    securityRules: [
      {
        name: 'AllowVnetInbound'
        properties: {
          protocol: '*'
          sourcePortRange: '*'
          sourceAddressPrefix: 'VirtualNetwork'
          destinationPortRange: '*'
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 100
          direction: 'Inbound'
        }
      }
      {
        name: 'AllowAzureLoadBalancerInbound'
        properties: {
          protocol: '*'
          sourcePortRange: '*'
          sourceAddressPrefix: 'AzureLoadBalancer'
          destinationPortRange: '*'
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 110
          direction: 'Inbound'
        }
      }
      {
        name: 'AllowP2SVpnInbound'
        properties: {
          protocol: 'Tcp'
          sourcePortRange: '*'
          sourceAddressPrefix: '172.16.201.0/24'
          destinationPortRange: '443'
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 120
          direction: 'Inbound'
        }
      }
      {
        name: 'DenyInternetInbound'
        properties: {
          protocol: '*'
          sourcePortRange: '*'
          sourceAddressPrefix: 'Internet'
          destinationPortRange: '*'
          destinationAddressPrefix: '*'
          access: 'Deny'
          priority: 4096
          direction: 'Inbound'
        }
      }
      {
        name: 'AllowVnetOutbound'
        properties: {
          protocol: '*'
          sourcePortRange: '*'
          sourceAddressPrefix: '*'
          destinationPortRange: '*'
          destinationAddressPrefix: 'VirtualNetwork'
          access: 'Allow'
          priority: 100
          direction: 'Outbound'
        }
      }
      {
        name: 'AllowAzureCloudOutbound'
        properties: {
          protocol: 'Tcp'
          sourcePortRange: '*'
          sourceAddressPrefix: '*'
          destinationPortRange: '443'
          destinationAddressPrefix: 'AzureCloud'
          access: 'Allow'
          priority: 110
          direction: 'Outbound'
        }
      }
      {
        name: 'AllowP2SVpnOutbound'
        properties: {
          protocol: 'Tcp'
          sourcePortRange: '*'
          sourceAddressPrefix: '*'
          destinationPortRange: '*'
          destinationAddressPrefix: '172.16.201.0/24'
          access: 'Allow'
          priority: 125
          direction: 'Outbound'
        }
      }
      {
        name: 'AllowDnsOutbound'
        properties: {
          protocol: '*'
          sourcePortRange: '*'
          sourceAddressPrefix: '*'
          destinationPortRange: '53'
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 120
          direction: 'Outbound'
        }
      }
      {
        name: 'DenyInternetOutbound'
        properties: {
          protocol: '*'
          sourcePortRange: '*'
          sourceAddressPrefix: '*'
          destinationPortRange: '*'
          destinationAddressPrefix: 'Internet'
          access: 'Deny'
          priority: 4096
          direction: 'Outbound'
        }
      }
    ]
  }
}

module applicationGatewayNSG 'br/public:avm/res/network/network-security-group:0.5.1' = {
  name: 'application-gateway-nsg'
  params: {
    name: '${vnetName}-application-gateway-subnet-nsg-${location}'
    location: location
    tags: tags
    securityRules: [
      {
        name: 'AllowInternetHttp'
        properties: {
          protocol: 'Tcp'
          sourcePortRange: '*'
          sourceAddressPrefix: 'Internet'
          destinationPortRange: '80'
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 300
          direction: 'Inbound'
        }
      }
      {
        name: 'AllowGatewayManagerInbound'
        properties: {
          protocol: '*'
          sourcePortRange: '*'
          sourceAddressPrefix: 'GatewayManager'
          destinationPortRange: '65200-65535'
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 2702
          direction: 'Inbound'
        }
      }
    ]
  }
}

module vnet 'br/public:avm/res/network/virtual-network:0.6.1' = {
  name: 'vnet'
  params: {
    name: vnetName
    location: location
    tags: tags
    addressPrefixes: ['10.0.0.0/16']
    subnets: [
      {
        name: privateEndpointsSubnetName
        addressPrefix: '10.0.8.0/24'
        privateEndpointNetworkPolicies: 'Disabled'
        privateLinkServiceNetworkPolicies: 'Enabled'
        networkSecurityGroupResourceId: privateEndpointsNSG.outputs.resourceId
      }
      {
        // AGIC with Azure CNI Overlay requires a dedicated subnet no larger than /24.
        name: applicationGatewaySubnetName
        addressPrefix: '10.0.12.0/24'
        delegation: 'Microsoft.Network/applicationGateways'
        networkSecurityGroupResourceId: applicationGatewayNSG.outputs.resourceId
      }
      {
        name: aksSubnetName
        addressPrefix: '10.0.16.0/20'
      }
      {
        name: gatewaySubnetName
        addressPrefix: '10.0.255.0/27'
      }
      {
        name: dnsResolverSubnetName
        addressPrefix: '10.0.11.0/28'
        delegation: 'Microsoft.Network/dnsResolvers'
      }
    ]
  }
}

var privateEndpointsSubnetIndex = indexOf(vnet.outputs.subnetNames, privateEndpointsSubnetName)
var applicationGatewaySubnetIndex = indexOf(vnet.outputs.subnetNames, applicationGatewaySubnetName)
var aksSubnetIndex = indexOf(vnet.outputs.subnetNames, aksSubnetName)
var dnsResolverSubnetIndex = indexOf(vnet.outputs.subnetNames, dnsResolverSubnetName)

module virtualNetworkGateway 'br/public:avm/res/network/virtual-network-gateway:0.8.0' = if (useVpnGateway) {
  name: 'virtual-network-gateway'
  params: {
    name: vpnGatewayName
    clusterSettings: {
      clusterMode: 'activePassiveNoBgp'
    }
    gatewayType: 'Vpn'
    virtualNetworkResourceId: vnet.outputs.resourceId
    // VpnGw1AZ is Generation1-only; it still supports P2S OpenVPN with Entra auth (250 connections).
    vpnGatewayGeneration: 'Generation1'
    vpnClientAddressPoolPrefix: '172.16.201.0/24'
    skuName: 'VpnGw1AZ'
    vpnClientAadConfiguration: {
      aadAudience: 'c632b3df-fb67-4d84-bdcf-b95ad541b5c8'
      aadIssuer: 'https://sts.windows.net/${tenant().tenantId}/'
      aadTenant: '${environment().authentication.loginEndpoint}${tenant().tenantId}'
      vpnAuthenticationTypes: ['AAD']
      vpnClientProtocols: ['OpenVPN']
    }
  }
}

module dnsResolver 'br/public:avm/res/network/dns-resolver:0.5.4' = if (useVpnGateway) {
  name: 'dns-resolver'
  params: {
    name: dnsResolverName
    location: location
    virtualNetworkResourceId: vnet.outputs.resourceId
    inboundEndpoints: [
      {
        name: 'inboundEndpoint'
        subnetResourceId: vnet.outputs.subnetResourceIds[dnsResolverSubnetIndex]
      }
    ]
  }
}

output privateEndpointsSubnetId string = vnet.outputs.subnetResourceIds[privateEndpointsSubnetIndex]
output applicationGatewaySubnetId string = vnet.outputs.subnetResourceIds[applicationGatewaySubnetIndex]
output aksSubnetId string = vnet.outputs.subnetResourceIds[aksSubnetIndex]
output privateDnsResolverSubnetId string = useVpnGateway ? vnet.outputs.subnetResourceIds[dnsResolverSubnetIndex] : ''
output vnetName string = vnet.outputs.name
output vnetId string = vnet.outputs.resourceId
output virtualNetworkGatewayId string = useVpnGateway ? virtualNetworkGateway!.outputs.resourceId : ''
