targetScope = 'resourceGroup'

@description('Existing storage account created by main.bicep.')
param storageAccountName string

@description('Existing RBAC-enabled Key Vault created by main.bicep.')
param keyVaultName string

@description('Object/principal ID of the existing VM system-assigned managed identity. This is not the VM resource ID or application/client ID.')
param vmPrincipalId string

@description('Assign Key Vault Secrets User now. Set false while the protected-file pilot is in use; Blob roles are always assigned.')
param assignKeyVaultSecretsUser bool = true

var storageBlobDataContributorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
)
var keyVaultSecretsUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '4633458b-17de-408a-b874-0445c86b69e6'
)

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: storageAccount
  name: 'default'
}

var writableContainerNames = [
  'raw-pdfs'
  'raw-emails'
  'pdfco-json'
  'db-backups'
]

resource writableContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = [for containerName in writableContainerNames: {
  parent: blobService
  name: containerName
}]

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource vmStorageBlobDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (containerName, index) in writableContainerNames: {
  name: guid(writableContainers[index].id, vmPrincipalId, storageBlobDataContributorRoleId)
  scope: writableContainers[index]
  properties: {
    principalId: vmPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: storageBlobDataContributorRoleId
  }
}]

resource vmKeyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (assignKeyVaultSecretsUser) {
  name: guid(keyVault.id, vmPrincipalId, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    principalId: vmPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyVaultSecretsUserRoleId
  }
}

output vmPrincipalId string = vmPrincipalId
output storageRoleAssignmentIds array = [for (containerName, index) in writableContainerNames: vmStorageBlobDataContributor[index].id]
output keyVaultRoleAssignmentId string = assignKeyVaultSecretsUser ? vmKeyVaultSecretsUser.id : ''
