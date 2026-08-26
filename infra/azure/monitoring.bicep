targetScope = 'resourceGroup'

@description('Azure region of the existing VM and Log Analytics workspace.')
param location string = resourceGroup().location

@description('Name of the existing COI VM. This template onboards it; it never creates or replaces the VM.')
param existingVmName string = 'vm-coi-portal-dev-01'

@description('Existing Log Analytics workspace created by main.bicep.')
param logAnalyticsWorkspaceName string = 'log-coi-portal-dev-01'

@description('Data collection rule name.')
param dataCollectionRuleName string = 'dcr-coi-portal-dev-ingestion-01'

@minValue(30)
@maxValue(730)
@description('Analytics-table retention. Log ingestion and retention incur Azure Monitor charges.')
param logRetentionDays int = 30

@description('Additional non-secret resource tags.')
param additionalTags object = {}

var commonTags = union({
  workload: 'coi-portal'
  environment: 'dev'
  owner: 'COI'
  managedBy: 'Bicep'
  dataClassification: 'Confidential'
}, additionalTags)

resource existingVm 'Microsoft.Compute/virtualMachines@2024-03-01' existing = {
  name: existingVmName
}

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource ingestionLogTable 'Microsoft.OperationalInsights/workspaces/tables@2022-10-01' = {
  parent: logAnalyticsWorkspace
  name: 'COIIngestion_CL'
  properties: {
    plan: 'Analytics'
    retentionInDays: logRetentionDays
    schema: {
      name: 'COIIngestion_CL'
      columns: [
        {
          name: 'TimeGenerated'
          type: 'dateTime'
        }
        {
          name: 'Computer'
          type: 'string'
        }
        {
          name: 'FilePath'
          type: 'string'
        }
        {
          name: 'RawData'
          type: 'string'
        }
      ]
    }
    totalRetentionInDays: logRetentionDays
  }
}

resource ingestionDataCollectionRule 'Microsoft.Insights/dataCollectionRules@2024-03-11' = {
  name: dataCollectionRuleName
  location: location
  kind: 'Linux'
  properties: {
    dataFlows: [
      {
        streams: [
          'Custom-COIIngestion_CL'
        ]
        destinations: [
          'coiLogAnalytics'
        ]
        transformKql: 'source'
        outputStream: 'Custom-COIIngestion_CL'
      }
    ]
    dataSources: {
      logFiles: [
        {
          name: 'coiIngestionLog'
          streams: [
            'Custom-COIIngestion_CL'
          ]
          filePatterns: [
            '/var/log/coi/ingestion.log'
          ]
          format: 'text'
          settings: {
            text: {
              recordStartTimestampFormat: 'YYYY-MM-DD HH:MM:SS'
            }
          }
        }
      ]
    }
    destinations: {
      logAnalytics: [
        {
          name: 'coiLogAnalytics'
          workspaceResourceId: logAnalyticsWorkspace.id
        }
      ]
    }
    streamDeclarations: {
      'Custom-COIIngestion_CL': {
        columns: [
          {
            name: 'TimeGenerated'
            type: 'datetime'
          }
          {
            name: 'Computer'
            type: 'string'
          }
          {
            name: 'FilePath'
            type: 'string'
          }
          {
            name: 'RawData'
            type: 'string'
          }
        ]
      }
    }
  }
  tags: commonTags
  dependsOn: [
    ingestionLogTable
  ]
}

resource azureMonitorAgent 'Microsoft.Compute/virtualMachines/extensions@2024-03-01' = {
  parent: existingVm
  name: 'AzureMonitorLinuxAgent'
  location: location
  properties: {
    publisher: 'Microsoft.Azure.Monitor'
    type: 'AzureMonitorLinuxAgent'
    typeHandlerVersion: '1.0'
    autoUpgradeMinorVersion: true
    enableAutomaticUpgrade: true
  }
}

resource vmDataCollectionAssociation 'Microsoft.Insights/dataCollectionRuleAssociations@2023-03-11' = {
  name: 'assoc-coi-ingestion-log'
  scope: existingVm
  properties: {
    dataCollectionRuleId: ingestionDataCollectionRule.id
    description: 'Collects the COI ingestion text log from the existing VM.'
  }
  dependsOn: [
    azureMonitorAgent
  ]
}

output dataCollectionRuleId string = ingestionDataCollectionRule.id
output customLogTableName string = ingestionLogTable.name
output vmResourceId string = existingVm.id
