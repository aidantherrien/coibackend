targetScope = 'resourceGroup'

@description('Azure region. The delegated subnet, virtual network, and server must be in this region.')
param location string = resourceGroup().location

@description('Globally unique Azure Database for PostgreSQL Flexible Server name.')
@minLength(3)
@maxLength(63)
param serverName string

@allowed([
  'dev'
  'test'
  'prod'
])
@description('Deployment environment used in names and tags. This template is a later production gate, so prod is the default.')
param environmentName string = 'prod'

@description('Full resource ID of the existing virtual network containing both the VM and the dedicated database subnet.')
param virtualNetworkResourceId string

@description('Full resource ID of an empty subnet delegated only to Microsoft.DBforPostgreSQL/flexibleServers. A /28 is the minimum; use a larger subnet for growth.')
param delegatedSubnetResourceId string

@description('Private DNS zone ending in .postgres.database.azure.com. It must not equal the server name.')
param privateDnsZoneName string = 'coi-${environmentName}.postgres.database.azure.com'

@description('Initial server administrator login. Do not use this login for deployment migrations or the runtime service.')
param administratorLogin string = 'coi_platform_admin'

@secure()
@description('Initial server administrator password. Supply interactively or from an approved secret source; never commit it.')
param administratorLoginPassword string

@description('PostgreSQL database created for the application.')
param databaseName string = 'coi'

@allowed([
  'Burstable'
  'GeneralPurpose'
  'MemoryOptimized'
])
@description('Flexible Server compute tier. Burstable is a cost-conscious managed starting point, not a production sizing recommendation; size from measured load and recovery requirements.')
param skuTier string = 'Burstable'

@description('Region-supported Flexible Server SKU name. Confirm availability and cost immediately before deployment.')
param skuName string = 'Standard_B1ms'

@minValue(32)
@maxValue(65536)
@description('Provisioned storage size in GiB. Storage can grow automatically and cannot be reduced in place.')
param storageSizeGB int = 32

@minValue(7)
@maxValue(35)
@description('Point-in-time backup retention in days.')
param backupRetentionDays int = 7

@allowed([
  'Disabled'
  'SameZone'
  'ZoneRedundant'
])
@description('Reviewed high-availability mode. ZoneRedundant and SameZone availability and cost depend on region/SKU.')
param highAvailabilityMode string = 'Disabled'

@allowed([
  'Disabled'
  'Enabled'
])
@description('Reviewed geo-redundant backup setting. Enable only after confirming regional support, recovery requirements, and cost.')
param geoRedundantBackup string = 'Disabled'

@description('Protect the server from accidental deletion with an Azure CanNotDelete lock.')
param enableDeleteLock bool = true

@description('Additional non-secret resource tags.')
param additionalTags object = {}

var commonTags = union({
  workload: 'coi-portal'
  environment: environmentName
  owner: 'COI'
  managedBy: 'Bicep'
  dataClassification: 'Confidential'
}, additionalTags)

resource privateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: privateDnsZoneName
  location: 'global'
  tags: commonTags
}

resource privateDnsVnetLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: privateDnsZone
  name: 'link-coi-postgresql-vnet'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetworkResourceId
    }
  }
}

resource flexibleServer 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: serverName
  location: location
  sku: {
    name: skuName
    tier: skuTier
  }
  properties: {
    administratorLogin: administratorLogin
    administratorLoginPassword: administratorLoginPassword
    authConfig: {
      activeDirectoryAuth: 'Disabled'
      passwordAuth: 'Enabled'
    }
    backup: {
      backupRetentionDays: backupRetentionDays
      geoRedundantBackup: geoRedundantBackup
    }
    createMode: 'Create'
    highAvailability: {
      mode: highAvailabilityMode
    }
    maintenanceWindow: {
      customWindow: 'Enabled'
      dayOfWeek: 0
      startHour: 5
      startMinute: 0
    }
    network: {
      delegatedSubnetResourceId: delegatedSubnetResourceId
      privateDnsZoneArmResourceId: privateDnsZone.id
    }
    storage: {
      autoGrow: 'Enabled'
      storageSizeGB: storageSizeGB
    }
    version: '16'
  }
  tags: commonTags
  dependsOn: [
    privateDnsVnetLink
  ]
}

resource applicationDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: flexibleServer
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

resource serverDeleteLock 'Microsoft.Authorization/locks@2020-05-01' = if (enableDeleteLock) {
  name: 'lock-delete-${flexibleServer.name}'
  scope: flexibleServer
  properties: {
    level: 'CanNotDelete'
    notes: 'Protects the authoritative COI PostgreSQL server from accidental deletion.'
  }
}

output serverName string = flexibleServer.name
output serverId string = flexibleServer.id
output serverFqdn string = flexibleServer.properties.fullyQualifiedDomainName
output databaseName string = applicationDatabase.name
output privateDnsZoneId string = privateDnsZone.id
