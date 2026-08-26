targetScope = 'resourceGroup'

@description('Azure region for all regional resources. Keep this aligned with the existing VM.')
param location string = resourceGroup().location

@description('Short workload name used in Microsoft-style resource names and tags.')
param workloadName string = 'coi-portal'

@allowed([
  'dev'
  'test'
  'prod'
])
@description('Deployment environment.')
param environmentName string = 'dev'

@description('Globally unique GPv2 storage account name. Storage account names must be lowercase and contain no hyphens.')
@minLength(3)
@maxLength(24)
param storageAccountName string = toLower('stcoi${take('${environmentName}${uniqueString(subscription().id, resourceGroup().id)}', 19)}')

@description('Globally unique Key Vault name.')
@minLength(3)
@maxLength(24)
param keyVaultName string = toLower('kv-${take('${workloadName}-${environmentName}-${uniqueString(resourceGroup().id)}', 21)}')

@description('Log Analytics workspace name. The separate monitoring template onboards the existing VM only when explicitly deployed.')
param logAnalyticsWorkspaceName string = 'log-${workloadName}-${environmentName}-01'

@allowed([
  'Enabled'
  'Disabled'
])
@description('Keep Enabled for the pilot unless private endpoints and private DNS already exist. Authentication and anonymous access controls still apply.')
param storagePublicNetworkAccess string = 'Enabled'

@allowed([
  'Enabled'
  'Disabled'
])
@description('Keep Enabled for the pilot unless a Key Vault private endpoint and DNS path already exist.')
param keyVaultPublicNetworkAccess string = 'Enabled'

@minValue(7)
@maxValue(365)
@description('Retention for deleted blobs and deleted containers.')
param blobSoftDeleteRetentionDays int = 30

@allowed([
  30
  60
  90
  120
  180
  365
  730
])
@description('Default Log Analytics retention. Log ingestion and retention can incur charges.')
param logRetentionDays int = 30

@description('Protect the storage account and Key Vault from accidental deletion. Remove locks only through an approved retirement procedure.')
param enableDeleteLocks bool = true

@description('Additional non-secret resource tags.')
param additionalTags object = {}

var commonTags = union({
  workload: workloadName
  environment: environmentName
  owner: 'COI'
  managedBy: 'Bicep'
  dataClassification: 'Confidential'
}, additionalTags)

var containerNames = [
  'raw-pdfs'
  'raw-emails'
  'pdfco-json'
  'db-backups'
]

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    isHnsEnabled: false
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: storagePublicNetworkAccess
    supportsHttpsTrafficOnly: true
    networkAcls: {
      bypass: 'None'
      defaultAction: storagePublicNetworkAccess == 'Enabled' ? 'Allow' : 'Deny'
    }
    encryption: {
      keySource: 'Microsoft.Storage'
      requireInfrastructureEncryption: false
      services: {
        blob: {
          enabled: true
          keyType: 'Account'
        }
        file: {
          enabled: true
          keyType: 'Account'
        }
      }
    }
  }
  tags: commonTags
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    containerDeleteRetentionPolicy: {
      allowPermanentDelete: false
      days: blobSoftDeleteRetentionDays
      enabled: true
    }
    deleteRetentionPolicy: {
      allowPermanentDelete: false
      days: blobSoftDeleteRetentionDays
      enabled: true
    }
    isVersioningEnabled: true
  }
}

resource blobContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for containerName in containerNames: {
  parent: blobService
  name: containerName
  properties: {
    publicAccess: 'None'
  }
}]

resource storageDeleteLock 'Microsoft.Authorization/locks@2020-05-01' = if (enableDeleteLocks) {
  name: 'lock-delete-${storageAccount.name}'
  scope: storageAccount
  properties: {
    level: 'CanNotDelete'
    notes: 'Protects COI source documents, parser evidence, and database backups from accidental account deletion.'
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    accessPolicies: []
    enablePurgeProtection: true
    enableRbacAuthorization: true
    enableSoftDelete: true
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: false
    publicNetworkAccess: keyVaultPublicNetworkAccess
    sku: {
      family: 'A'
      name: 'standard'
    }
    softDeleteRetentionInDays: 90
    tenantId: tenant().tenantId
    networkAcls: {
      bypass: 'None'
      defaultAction: keyVaultPublicNetworkAccess == 'Enabled' ? 'Allow' : 'Deny'
    }
  }
  tags: commonTags
}

resource keyVaultDeleteLock 'Microsoft.Authorization/locks@2020-05-01' = if (enableDeleteLocks) {
  name: 'lock-delete-${keyVault.name}'
  scope: keyVault
  properties: {
    level: 'CanNotDelete'
    notes: 'Protects the COI secret store from accidental deletion. Purge protection remains enabled independently.'
  }
}

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsWorkspaceName
  location: location
  properties: {
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
    retentionInDays: logRetentionDays
    sku: {
      name: 'PerGB2018'
    }
  }
  tags: commonTags
}

output storageAccountName string = storageAccount.name
output storageAccountId string = storageAccount.id
output storageBlobEndpoint string = storageAccount.properties.primaryEndpoints.blob
output blobContainerNames array = containerNames
output keyVaultName string = keyVault.name
output keyVaultId string = keyVault.id
output keyVaultUri string = keyVault.properties.vaultUri
output logAnalyticsWorkspaceName string = logAnalyticsWorkspace.name
output logAnalyticsWorkspaceId string = logAnalyticsWorkspace.id
