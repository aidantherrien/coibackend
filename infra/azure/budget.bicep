targetScope = 'subscription'

@description('Monthly budget name.')
param budgetName string = 'budget-coi-portal-dev-monthly'

@description('Existing COI resource group whose costs this budget filters.')
param resourceGroupName string = 'rg-coi-portal-dev'

@minValue(1)
@description('Monthly budget amount in the subscription billing currency. Budgets notify but do not stop resources or spending.')
param monthlyBudgetAmount int = 50

@minLength(1)
@description('Operator and owner email addresses that receive the 50%, 80%, and 100% actual-cost alerts.')
param budgetContactEmails array

@description('Budget start on the first day of the current month. Override only with another first-of-month UTC timestamp accepted by Cost Management.')
param budgetStartDate string = utcNow('yyyy-MM-01T00:00:00Z')

resource monthlyBudget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: budgetName
  properties: {
    amount: monthlyBudgetAmount
    category: 'Cost'
    filter: {
      dimensions: {
        name: 'ResourceGroupName'
        operator: 'In'
        values: [
          resourceGroupName
        ]
      }
    }
    notifications: {
      Actual_GreaterThan_50_Percent: {
        contactEmails: budgetContactEmails
        contactGroups: []
        contactRoles: []
        enabled: true
        locale: 'en-us'
        operator: 'GreaterThan'
        threshold: 50
        thresholdType: 'Actual'
      }
      Actual_GreaterThan_80_Percent: {
        contactEmails: budgetContactEmails
        contactGroups: []
        contactRoles: []
        enabled: true
        locale: 'en-us'
        operator: 'GreaterThan'
        threshold: 80
        thresholdType: 'Actual'
      }
      Actual_GreaterThan_100_Percent: {
        contactEmails: budgetContactEmails
        contactGroups: []
        contactRoles: []
        enabled: true
        locale: 'en-us'
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Actual'
      }
    }
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: budgetStartDate
    }
  }
}

output budgetId string = monthlyBudget.id
