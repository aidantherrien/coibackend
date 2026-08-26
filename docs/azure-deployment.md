# Azure deployment runbook

This runbook prepares the existing Ubuntu LTS VM `vm-coi-portal-dev-01` in
`rg-coi-portal-dev`; it does not create or replace that VM. The first controlled
pilot uses PostgreSQL 16 on the VM, Azure Blob Storage as the durable evidence
store, and either a root-protected environment file or Azure Key Vault for
secrets. Azure Database for PostgreSQL Flexible Server is a separate, later
production gate.

Nothing in this repository deploys itself. `what-if` first, review the price and
network changes, obtain approval, and only then run a `create` command. Storage,
Key Vault operations, Log Analytics ingestion, the monitoring agent, and
Flexible Server can all incur charges.

## Deployment boundaries

The Azure assets are intentionally separated:

| File | Purpose | Existing-VM impact |
| --- | --- | --- |
| `infra/azure/main.bicep` | Private GPv2 Blob containers, RBAC Key Vault, Log Analytics, and delete locks | None |
| `infra/azure/rbac.bicep` | Container-scoped Blob roles and optional Key Vault read role for the VM system identity | Role assignments only |
| `infra/azure/budget.bicep` | Resource-group-filtered monthly cost alerts | None; alerts do not stop spend |
| `infra/azure/monitoring.bicep` | Azure Monitor Agent, custom text-log table, DCR, and VM association | Adds/updates one VM extension |
| `infra/azure/postgresql.bicep` | Later private PostgreSQL 16 server, private DNS, database, and delete lock | None, but requires reachable existing network resources |

The baseline storage account has secure transfer required, minimum TLS 1.2,
anonymous Blob access disabled, Shared Key authorization disabled, Microsoft
Entra authentication as the default, service encryption, blob versioning,
30-day blob/container soft delete, and a `CanNotDelete` account lock. The exact
containers are `raw-pdfs`, `raw-emails`, `pdfco-json`, and `db-backups`.
Microsoft recommends combining versioning and soft delete; retained versions
also consume storage, so review version counts and cost before adding any
automatic lifecycle deletion policy. The current template deliberately never
expires authoritative files. See Microsoft's guidance for [blob soft delete
and versioning](https://learn.microsoft.com/en-us/azure/storage/blobs/soft-delete-blob-overview)
and [preventing Shared Key authorization](https://learn.microsoft.com/en-us/azure/storage/common/shared-key-authorization-prevent).

The `CanNotDelete` lock protects the storage account control plane, not blobs
deleted through the data plane. Versioning and soft delete provide that
recovery window. [Azure resource-lock semantics](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/lock-resources)
make this distinction explicit.

Storage and Key Vault public service endpoints default to `Enabled` for the
existing-VM pilot, but every request still requires Microsoft Entra/RBAC and
anonymous Blob access is disabled. This is not a public application or database
port. Set either endpoint to `Disabled` only after a private endpoint, private
DNS, and a tested VM route exist; this package does not guess at the existing
network topology.

## 1. Preflight the account and existing VM

Use a current Azure CLI from a trusted operator machine or Azure Cloud Shell.
The commands below use Bash syntax. Set values from the actual subscription;
never assume that the example region in a parameter file matches the VM.

```bash
az login
az account set --subscription '<subscription name or id>'
az account show --query '{subscription:name, subscriptionId:id, tenantId:tenantId}' -o yaml

RG=rg-coi-portal-dev
VM=vm-coi-portal-dev-01
VM_LOCATION=$(az vm show --resource-group "$RG" --name "$VM" --query location -o tsv)
VM_ID=$(az vm show --resource-group "$RG" --name "$VM" --query id -o tsv)
printf 'VM location: %s\nVM id: %s\n' "$VM_LOCATION" "$VM_ID"
```

Stop if the subscription, tenant, resource group, VM name, or region is wrong.
Record current SKU, power state, identity, network interfaces, and effective
NSG before changing anything:

```bash
az vm show --resource-group "$RG" --name "$VM" \
  --query '{name:name,location:location,size:hardwareProfile.vmSize,identity:identity.type,nics:networkProfile.networkInterfaces[].id}' -o yaml
az vm get-instance-view --resource-group "$RG" --name "$VM" \
  --query 'instanceView.statuses[].displayStatus' -o tsv
NIC_ID=$(az vm show --resource-group "$RG" --name "$VM" \
  --query 'networkProfile.networkInterfaces[0].id' -o tsv)
az network nic list-effective-nsg --ids "$NIC_ID" -o jsonc
```

Keep SSH limited to the known operator source IP. There must be no Internet
inbound allow rule for TCP 5432, 8000, 3000, 80, or 443. This repository installs
no web listener. On the VM, confirm listeners independently:

```bash
sudo ss -lntup
sudo ufw status verbose
```

Outbound HTTPS must reach Azure identity, Blob, Key Vault when enabled, package
repositories during release installation, PDF.co, and the provider's configured
result hosts. The managed-identity metadata endpoint is link-local and must also
remain reachable. Do not print or log its access token.

Register only the providers required for the phases being approved:

```bash
for provider in Microsoft.Storage Microsoft.KeyVault Microsoft.OperationalInsights \
  Microsoft.Authorization Microsoft.Insights Microsoft.Compute Microsoft.Consumption
do
  az provider register --namespace "$provider"
done

az provider list --query "[?namespace=='Microsoft.Storage' || namespace=='Microsoft.KeyVault' || namespace=='Microsoft.OperationalInsights' || namespace=='Microsoft.Authorization' || namespace=='Microsoft.Insights' || namespace=='Microsoft.Compute' || namespace=='Microsoft.Consumption'].{provider:namespace,state:registrationState}" -o table
```

Provider registration and resource locks require suitable control-plane
permissions. Role assignments require `Microsoft.Authorization/roleAssignments/write`,
normally Owner, User Access Administrator, or Role Based Access Control
Administrator at the relevant scope; Contributor alone cannot grant these
roles. Microsoft documents the [managed-identity VM setup](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/how-to-configure-managed-identities)
and [container-level Blob role scope](https://learn.microsoft.com/en-us/azure/storage/blobs/assign-azure-role-data-access).

## 2. Validate and review Bicep

Copy each `.parameters.example.json` used in a deployment to an operator-owned
path outside the repository. Replace every example or sentinel value, especially
region, subscription IDs, globally unique names, tags, identity object ID, and
budget contacts. Never put a password or API key in a committed parameter file.

Build every template before an account-specific validation:

```bash
az bicep upgrade
for template in infra/azure/*.bicep; do
  az bicep build --file "$template" --stdout >/dev/null
done
```

For the platform baseline, use an edited copy of
`main.parameters.example.json` and keep its location equal to `VM_LOCATION`:

```bash
MAIN_PARAMETERS=/secure/operator/path/main.parameters.json
az deployment group validate --resource-group "$RG" \
  --template-file infra/azure/main.bicep \
  --parameters @"$MAIN_PARAMETERS"
az deployment group what-if --resource-group "$RG" \
  --name coi-platform-dev-01 \
  --template-file infra/azure/main.bicep \
  --parameters @"$MAIN_PARAMETERS"
```

Review names, region, tags, endpoint settings, retention, SKU, Log Analytics
retention, and both delete locks. A `what-if` is not a cost estimate. Obtain an
Azure Pricing Calculator estimate and approval before this billable deployment:

```bash
az deployment group create --resource-group "$RG" \
  --name coi-platform-dev-01 \
  --template-file infra/azure/main.bicep \
  --parameters @"$MAIN_PARAMETERS"
az deployment group show --resource-group "$RG" --name coi-platform-dev-01 \
  --query properties.outputs -o jsonc
```

Capture the output names without copying secrets (the template has none):

```bash
STORAGE_ACCOUNT=$(az deployment group show --resource-group "$RG" --name coi-platform-dev-01 \
  --query 'properties.outputs.storageAccountName.value' -o tsv)
KEY_VAULT=$(az deployment group show --resource-group "$RG" --name coi-platform-dev-01 \
  --query 'properties.outputs.keyVaultName.value' -o tsv)
WORKSPACE=$(az deployment group show --resource-group "$RG" --name coi-platform-dev-01 \
  --query 'properties.outputs.logAnalyticsWorkspaceName.value' -o tsv)
printf 'Storage: %s\nKey Vault: %s\nWorkspace: %s\n' "$STORAGE_ACCOUNT" "$KEY_VAULT" "$WORKSPACE"
```

## 3. Configure budget alerts

Edit a private copy of `budget.parameters.example.json` with the real operator
and owner email addresses. The default is a monthly 50-unit budget in the
subscription billing currency, filtered to `rg-coi-portal-dev`, with actual-cost
alerts at 50%, 80%, and 100%. Budgets notify; they do not suspend the VM or cap
spend. Cost data can be delayed, and a new subscription might not support a
budget immediately. See the official [Bicep budget quickstart](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/quick-create-budget-bicep).

```bash
BUDGET_PARAMETERS=/secure/operator/path/budget.parameters.json
az deployment sub validate --location "$VM_LOCATION" \
  --template-file infra/azure/budget.bicep \
  --parameters @"$BUDGET_PARAMETERS"
az deployment sub what-if --location "$VM_LOCATION" \
  --name coi-budget-dev-01 \
  --template-file infra/azure/budget.bicep \
  --parameters @"$BUDGET_PARAMETERS"
az deployment sub create --location "$VM_LOCATION" \
  --name coi-budget-dev-01 \
  --template-file infra/azure/budget.bicep \
  --parameters @"$BUDGET_PARAMETERS"
```

Verify the deployment and send a separate human confirmation that both contact
addresses are current; an alert cannot be safely proven by waiting for spend.

```bash
az deployment sub show --name coi-budget-dev-01 --query properties.provisioningState -o tsv
```

Keep the VM's development auto-shutdown configured where practical and review
Cost Analysis weekly during the pilot.

## 4. Enable the VM identity and least-privilege data roles

Assigning a system identity mutates the existing VM resource but does not
recreate it. Skip the assignment if the VM already has one; otherwise obtain
approval and run:

```bash
az vm identity assign --resource-group "$RG" --name "$VM"
VM_PRINCIPAL_ID=$(az vm identity show --resource-group "$RG" --name "$VM" \
  --query principalId -o tsv)
printf 'VM principal object id: %s\n' "$VM_PRINCIPAL_ID"
```

Put that object ID and the deployed resource names in an external copy of
`rbac.parameters.example.json`. Set `assignKeyVaultSecretsUser` to `false` for
the protected-environment-file pilot or `true` when Key Vault secrets are ready.
The template assigns Storage Blob Data Contributor separately at each of the
four known container scopes, not at subscription, resource-group, or storage-
account scope.

```bash
RBAC_PARAMETERS=/secure/operator/path/rbac.parameters.json
az deployment group validate --resource-group "$RG" \
  --template-file infra/azure/rbac.bicep --parameters @"$RBAC_PARAMETERS"
az deployment group what-if --resource-group "$RG" --name coi-vm-rbac-dev-01 \
  --template-file infra/azure/rbac.bicep --parameters @"$RBAC_PARAMETERS"
az deployment group create --resource-group "$RG" --name coi-vm-rbac-dev-01 \
  --template-file infra/azure/rbac.bicep --parameters @"$RBAC_PARAMETERS"
```

Role assignment propagation can take several minutes. From the VM, request a
token without displaying it, then test one exact container. Set the account
name again in that VM shell; operator-machine variables do not transfer over
SSH. The application uses the same `DefaultAzureCredential` managed-identity
path and never needs an account key or connection string.

The `az` verification and backup commands require a current Azure CLI on the VM;
install it from Microsoft's [Linux Azure CLI instructions](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-linux)
and verify `az version`. The Python application itself does not require Azure
CLI because the Azure SDK obtains its managed-identity token directly.

```bash
STORAGE_ACCOUNT='<main.bicep storageAccountName output>'
curl --silent --show-error --noproxy '*' -H Metadata:true \
  'http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fstorage.azure.com%2F' \
  | python3 -c 'import json,sys; print("managed_identity_token_received=" + str(bool(json.load(sys.stdin).get("access_token"))))'

az login --identity
az storage blob list --account-name "$STORAGE_ACCOUNT" --container-name raw-pdfs \
  --auth-mode login --num-results 1 --query '[].{name:name,lastModified:properties.lastModified}' -o table
```

Do not use `--auth-mode key`, account keys, connection strings, or SAS tokens.
`AZURE_STORAGE_PREFIX` is an application naming prefix, not an authorization
boundary; RBAC is enforced at the four exact container resources.

For local development, `DefaultAzureCredential` can use the developer's own
`az login`, but authentication alone grants no Blob data access. Subscription
or resource-group Contributor is also not a Blob data role. A privileged access
administrator must separately approve the developer/operator identity and grant
Storage Blob Data Contributor (or Reader when writes are unnecessary) at only
the containers needed for that task. Assign only the matching Key Vault role—for
example, Secrets User for approved reads or Secrets Officer for the human secret
manager. Do not copy a VM token, share the VM identity, or broaden the VM's role
to make local development work; use the person's own auditable identity.

## 5. Verify Azure resource security

Run these checks after the platform and RBAC deployments:

```bash
az storage account show --resource-group "$RG" --name "$STORAGE_ACCOUNT" \
  --query '{httpsOnly:enableHttpsTrafficOnly,minTls:minimumTlsVersion,anonymousBlob:allowBlobPublicAccess,sharedKey:allowSharedKeyAccess,oauthDefault:defaultToOAuthAuthentication,publicNetwork:publicNetworkAccess}' -o yaml
az storage account blob-service-properties show --resource-group "$RG" \
  --account-name "$STORAGE_ACCOUNT" \
  --query '{versioning:isVersioningEnabled,blobSoftDelete:deleteRetentionPolicy,containerSoftDelete:containerDeleteRetentionPolicy}' -o yaml
az storage container-rm list --resource-group "$RG" --storage-account "$STORAGE_ACCOUNT" \
  --query '[].{name:name,publicAccess:properties.publicAccess}' -o table
az keyvault show --resource-group "$RG" --name "$KEY_VAULT" \
  --query '{rbac:properties.enableRbacAuthorization,purgeProtection:properties.enablePurgeProtection,softDeleteDays:properties.softDeleteRetentionInDays,publicNetwork:properties.publicNetworkAccess}' -o yaml
az lock list --resource-group "$RG" \
  --query '[].{name:name,level:level,scope:scope}' -o table
```

Expected results are HTTPS-only `true`, TLS `TLS1_2`, anonymous Blob and Shared
Key `false`, OAuth default `true`, versioning enabled, both soft-delete policies
enabled, exactly four private containers, Key Vault RBAC and purge protection
enabled, and delete locks on Storage and Key Vault.

## 6. Install an immutable application release

Build a release archive only from a reviewed revision. Exclude local secrets,
runtime data, and every PDF. Transfer it over the already restricted SSH path;
do not open an application port.

On the operator machine, package only a clean committed revision. The filename
check reads tracked names, not PDF contents, and refuses to package if runtime
data, a real `.env`, or any PDF was ever tracked:

```bash
set -euo pipefail
test -z "$(git status --porcelain)"
if git ls-files \
  | grep -E '(^|/)(data|var)/|(^|/)\.env|\.[Pp][Dd][Ff]$' \
  | grep -vE '(^|/)\.env\.example$'; then
  printf 'Refusing to package tracked data, environment, or PDF files.\n' >&2
  exit 1
fi
RELEASE_ID=$(git rev-parse --short=12 HEAD)
RELEASE_ARCHIVE="/tmp/coi-${RELEASE_ID}.tar.gz"
git archive --format=tar.gz --output="$RELEASE_ARCHIVE" HEAD
sha256sum "$RELEASE_ARCHIVE" > "${RELEASE_ARCHIVE}.sha256"
scp "$RELEASE_ARCHIVE" "${RELEASE_ARCHIVE}.sha256" \
  'azureuser@<restricted VM SSH endpoint>:/tmp/'
```

Inventory the VM before installing anything. The application requires Python
3.12-3.14. The local pilot requires PostgreSQL 16 server and client tools
including `pg_dump`; operations in this runbook also require systemd,
`logrotate`, and Azure CLI. The Azure CLI is not an application runtime
dependency.

```bash
. /etc/os-release
printf 'OS: %s %s\n' "$NAME" "$VERSION_ID"
for command_name in git python3 psql pg_dump pg_isready systemctl systemd-analyze logrotate az; do
  if command -v "$command_name" >/dev/null 2>&1; then
    printf '%-18s %s\n' "$command_name" "$(command -v "$command_name")"
  else
    printf '%-18s MISSING\n' "$command_name"
  fi
done
python3 -c 'import sys; print(sys.version); raise SystemExit(0 if (3,12) <= sys.version_info[:2] < (3,15) else 1)'
python3 -m pip --version
python3 -m venv --help >/dev/null
psql --version
pg_dump --version
systemctl --version | head -n 1
logrotate --version
az version
if getent passwd postgres >/dev/null && pg_isready -q; then
  sudo -u postgres psql -d postgres -Atqc 'SHOW server_version;'
fi
```

Stop if Python is outside the supported range, the server/client `psql` or
`pg_dump` major is not 16, or the service manager is not systemd. Do not
reinstall working packages blindly. On Ubuntu, calculate the missing package
set, inspect candidate versions, and install only after reviewing that output:

```bash
required_packages=(git python3 python3-pip python3-venv postgresql-16 postgresql-client-16 postgresql-contrib logrotate)
missing_packages=()
for package_name in "${required_packages[@]}"; do
  dpkg-query -W -f='${Status}' "$package_name" 2>/dev/null | grep -q 'ok installed' \
    || missing_packages+=("$package_name")
done
if ((${#missing_packages[@]})); then
  sudo apt-get update
  apt-cache policy "${missing_packages[@]}"
  read -r -p 'Install only the reviewed candidates above? [y/N] ' answer
  [[ "$answer" == y ]] && sudo apt-get install --no-install-recommends "${missing_packages[@]}"
fi
```

If the configured Ubuntu repositories do not offer PostgreSQL 16, stop and use
the [official PostgreSQL Ubuntu repository instructions](https://www.postgresql.org/download/linux/ubuntu/)
or approve an OS upgrade; do not silently accept another major. If Azure CLI is
missing, use Microsoft's [Azure CLI installation instructions](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-linux)
and rerun the inventory. Add compiler/build packages only if a reviewed locked
dependency must build from source.

On the VM, create stable service paths once:

```bash
id coi >/dev/null 2>&1 || sudo useradd --system --user-group \
  --home-dir /nonexistent --shell /usr/sbin/nologin coi
sudo install -d -o root -g root -m 0755 /opt/coi/releases
sudo install -d -o coi -g coi -m 0750 \
  /var/lib/coi/inbox/invoices /var/lib/coi/inbox/oa \
  /var/lib/coi/archive/invoices /var/lib/coi/archive/oa \
  /var/lib/coi/quarantine/invoices /var/lib/coi/quarantine/oa \
  /var/lib/coi/retry/invoice /var/lib/coi/retry/oa \
  /var/lib/coi/artifacts /var/log/coi
sudo install -d -o root -g root -m 0700 /var/lib/coi/backups
sudo touch /var/log/coi/ingestion.log
sudo chown coi:coi /var/log/coi/ingestion.log
sudo chmod 0640 /var/log/coi/ingestion.log
sudo install -d -o root -g root -m 0750 /etc/coi
```

On the VM, set the same reviewed ID, verify the transfer, and extract into a new
directory. Never extract over an existing bundle:

```bash
set -euo pipefail
RELEASE_ID='<reviewed release id>'
RELEASE_ARCHIVE="/tmp/coi-${RELEASE_ID}.tar.gz"
(cd /tmp && sha256sum --check "$(basename "${RELEASE_ARCHIVE}.sha256")")
RELEASE_DIR="/opt/coi/releases/$RELEASE_ID"
test ! -e "$RELEASE_DIR"
sudo install -d -o root -g root -m 0755 "$RELEASE_DIR"
sudo tar --extract --gzip --file="$RELEASE_ARCHIVE" --directory="$RELEASE_DIR" \
  --no-same-owner --no-same-permissions
```

For each reviewed release, place its extracted source at
`/opt/coi/releases/<release-id>`, owned by root and not writable by `coi`, then:

```bash
RELEASE_ID='<reviewed release id>'
RELEASE_DIR="/opt/coi/releases/$RELEASE_ID"
VENV_DIR="$RELEASE_DIR/.venv"
test -f "$RELEASE_DIR/requirements.lock"
test -f "$RELEASE_DIR/coi_backend/cli.py"
python3 --version
sudo python3 -m venv "$VENV_DIR"
sudo "$VENV_DIR/bin/python" -m pip --version
sudo "$VENV_DIR/bin/python" -m pip install -r "$RELEASE_DIR/requirements.lock"
sudo "$VENV_DIR/bin/python" -m compileall -q "$RELEASE_DIR/coi_backend"
sudo chown -R root:root "$RELEASE_DIR"
sudo chmod -R u=rwX,go=rX "$RELEASE_DIR"
```

The project requires Python 3.12 through 3.14. The fully resolved
`requirements.lock`, not `requirements.txt`, is the production install input.
Package installation uses outbound network access and must occur before a
restricted maintenance window if outbound policy is tightened. Code and its
release-local `.venv` form one immutable bundle. `/opt/coi/current` is the only
release pointer, so code and dependencies can never be split across two symlink
updates.

For the first deployment, switch `current` once after the bundle build, then
complete sections 7-9 before enabling timers:

```bash
sudo ln -sfnT "$RELEASE_DIR" /opt/coi/current
```

For every later update, build the new bundle completely before maintenance.
Stop only the timers so an already-running ingestion is allowed to finish; do
not stop or kill its service. Wait until both one-shots are inactive:

```bash
set -euo pipefail
sudo systemctl stop coi-ingest@invoice.timer coi-ingest@oa.timer
while systemctl is-active --quiet coi-ingest@invoice.service \
  || systemctl is-active --quiet coi-ingest@oa.service; do
  printf 'Waiting for active ingestion to finish...\n'
  sleep 5
done
```

Take and verify the section 11 backup. Apply pending migrations using the new
bundle and the deployment login before switching the pointer; for the local
pilot the migration step is:

```bash
set -euo pipefail
(cd "$RELEASE_DIR" && sudo -u coi-deploy env \
  DATABASE_URL='postgresql:///coi?host=/var/run/postgresql' \
  DATABASE_MIGRATION_ROLE=coi_owner \
  "$VENV_DIR/bin/python" -m coi_backend.cli migrate)
sudo -u postgres psql -d coi -v ON_ERROR_STOP=1 \
  -f "$RELEASE_DIR/sql/grants/least_privilege.sql"
```

For Flexible Server, use the separately protected deployment credential and the
section 13 role sequence. If migration or grants fail, leave the timers off and
do not switch. After success, atomically switch once, reinstall the service,
timer, and logrotate policy, run the transient runtime health check from section
9, and restart the timers only after it passes:

```bash
set -euo pipefail
sudo ln -sfnT "$RELEASE_DIR" /opt/coi/current
sudo install -o root -g root -m 0644 \
  /opt/coi/current/deploy/systemd/coi-ingest@.service \
  /etc/systemd/system/coi-ingest@.service
sudo install -o root -g root -m 0644 \
  /opt/coi/current/deploy/systemd/coi-ingest@.timer \
  /etc/systemd/system/coi-ingest@.timer
sudo install -o root -g root -m 0644 \
  /opt/coi/current/deploy/logrotate/coi /etc/logrotate.d/coi
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/coi-ingest@.service \
  /etc/systemd/system/coi-ingest@.timer
sudo logrotate --debug /etc/logrotate.d/coi
if sudo systemd-run --unit=coi-db-check-manual --wait --pipe --collect \
  --uid=coi --gid=coi --working-directory=/opt/coi/current \
  --property=EnvironmentFile=/etc/coi/coi.env \
  /opt/coi/current/.venv/bin/python -m coi_backend.cli check; then
  sudo systemctl start coi-ingest@invoice.timer coi-ingest@oa.timer
else
  printf 'Health check failed; timers remain stopped.\n' >&2
  exit 1
fi
```

The start command above is the final step, after the section 9 transient health
check. Keep the old bundle for rollback; never modify a retained bundle in
place.

## 7. PostgreSQL 16 local pilot smoke test

Local PostgreSQL is acceptable only for the controlled/redacted pilot. It is a
single-VM failure domain, so Blob-hosted dumps and restore drills are mandatory.
Do not expose TCP 5432 publicly. Prefer the Unix socket and PostgreSQL peer
authentication for the two local OS service accounts.

On a fresh PostgreSQL 16 instance, create separate deployment and runtime login
roles. The following one-time example assumes matching local OS accounts and
default peer authentication; adapt it to the audited `pg_hba.conf` rather than
weakening authentication globally:

```bash
id coi-deploy >/dev/null 2>&1 || sudo useradd --system --user-group \
  --home-dir /nonexistent --shell /usr/sbin/nologin coi-deploy
sudo -u postgres createuser --login --no-superuser --no-createdb --no-createrole coi-deploy
sudo -u postgres createuser --login --no-superuser --no-createdb --no-createrole coi
sudo -u postgres createdb --owner=postgres coi
```

If any role/database already exists, inspect it instead of rerunning creation.
Then preserve this exact security order:

```bash
sudo -u postgres psql -d coi -v ON_ERROR_STOP=1 \
  -f /opt/coi/current/sql/grants/bootstrap_roles.sql
sudo -u postgres psql -d coi -v ON_ERROR_STOP=1 \
  -c 'GRANT coi_migrator TO "coi-deploy";'

(cd /opt/coi/current && sudo -u coi-deploy env \
  DATABASE_URL='postgresql:///coi?host=/var/run/postgresql' \
  DATABASE_MIGRATION_ROLE=coi_owner \
  /opt/coi/current/.venv/bin/python -m coi_backend.cli migrate)

sudo -u postgres psql -d coi -v ON_ERROR_STOP=1 \
  -f /opt/coi/current/sql/grants/least_privilege.sql
sudo -u postgres psql -d coi -v ON_ERROR_STOP=1 \
  -c 'GRANT coi_runtime TO coi;'

(cd /opt/coi/current && sudo -u coi env \
  DATABASE_URL='postgresql:///coi?host=/var/run/postgresql' \
  /opt/coi/current/.venv/bin/python -m coi_backend.cli check)
```

`bootstrap_roles.sql` must be run by the database owner/administrator first.
Only the deployment login receives `coi_migrator`; the migration process alone
sets `DATABASE_MIGRATION_ROLE=coi_owner`. Run `least_privilege.sql` as admin
after migrations, then grant only `coi_runtime` to the application login. The
runtime environment must never contain `DATABASE_MIGRATION_ROLE` or membership
in `coi_owner`/`coi_migrator`. Runtime has SELECT-only access to
`coi.schema_migrations` for health checks.

## 8. Configure secrets and Azure Blob

Create the protected file, edit it with privilege, and never copy its content to
chat, tickets, logs, shell history, or source control:

```bash
sudo touch /etc/coi/coi.env
sudo chown root:root /etc/coi/coi.env
sudo chmod 0600 /etc/coi/coi.env
sudoedit /etc/coi/coi.env
sudo stat -c '%U %G %a %n' /etc/coi/coi.env
```

For the initial local-database/protected-file pilot, populate real values at
deployment time using this shape. The bracketed API-key instruction is not a
literal value:

```dotenv
DATABASE_URL=postgresql:///coi?host=/var/run/postgresql
DATABASE_CONNECT_TIMEOUT_SECONDS=10
PDFCO_API_KEY=<insert only in the protected VM file>
PDFCO_BASE_URL=https://api.pdf.co/v1

ARTIFACT_STORE=azure_blob
AZURE_STORAGE_ACCOUNT_URL=https://<storage-account-name>.blob.core.windows.net
AZURE_RAW_CONTAINER=raw-pdfs
AZURE_PARSER_CONTAINER=pdfco-json
AZURE_STORAGE_PREFIX=pilot

INVOICE_INPUT_DIR=/var/lib/coi/inbox/invoices
OA_INPUT_DIR=/var/lib/coi/inbox/oa
LOCAL_ARTIFACT_DIR=/var/lib/coi/artifacts
PROCESSING_STALE_AFTER_SECONDS=3600
LOG_LEVEL=INFO
```

Omit `AZURE_STORAGE_ENCRYPTION_SCOPE` unless a separately governed encryption
scope already exists. The baseline uses Microsoft-managed service encryption.
The systemd unit pins `ARTIFACT_STORE=azure_blob` at execution time, so an
incorrect environment file cannot silently downgrade retained evidence to the
VM-local artifact store.

Key Vault is the target after the pilot, but its availability does not block the
first run. When adopting it, a privileged operator grants themselves Key Vault
Secrets Officer at the vault scope, creates two secrets through an approved
secure input process, and redeploys `rbac.bicep` with
`assignKeyVaultSecretsUser=true`. The runtime needs only Key Vault Secrets User.
Microsoft recommends RBAC for Key Vault data access; see the [Key Vault RBAC
guide](https://learn.microsoft.com/en-us/azure/key-vault/general/rbac-guide).

Store each value as a non-empty raw string. A JSON object containing
`DATABASE_URL` for the database secret or `PDFCO_API_KEY` for the provider secret
is also supported. Configure only secret names and URLs in `/etc/coi/coi.env`:

Create or rotate values from protected mode-0600 input files on an encrypted
operator workstation, so the value is not placed in a command argument or shell
history. Remove those staging files using the workstation's approved secure
procedure after verifying the new Key Vault versions:

```bash
az keyvault secret set --vault-name "$KEY_VAULT" --name coi-database-url \
  --file /secure/operator/input/coi-database-url --encoding utf-8 --output none
az keyvault secret set --vault-name "$KEY_VAULT" --name coi-pdfco-api-key \
  --file /secure/operator/input/coi-pdfco-api-key --encoding utf-8 --output none
```

```dotenv
DATABASE_URL=
DATABASE_SECRET_NAME=coi-database-url
PDFCO_API_KEY=
PDFCO_API_KEY_SECRET_NAME=coi-pdfco-api-key
AZURE_KEY_VAULT_URL=https://<key-vault-name>.vault.azure.net/
```

The application fails closed if a direct value and its corresponding secret
name are both non-empty. It also fails if a secret name is configured without
`AZURE_KEY_VAULT_URL`. Verify secret metadata without reading secret values:

```bash
az keyvault secret show --vault-name "$KEY_VAULT" --name coi-database-url \
  --query '{id:id,enabled:attributes.enabled,updated:attributes.updated}' -o yaml
az keyvault secret show --vault-name "$KEY_VAULT" --name coi-pdfco-api-key \
  --query '{id:id,enabled:attributes.enabled,updated:attributes.updated}' -o yaml
```

Restarting a service is required to read rotated values. Retain the previous
enabled secret version until the new configuration has passed health and one
controlled ingestion.

## 9. Install and verify the scheduled service

Install the reviewed unit files and log rotation policy:

```bash
sudo install -o root -g root -m 0644 \
  /opt/coi/current/deploy/systemd/coi-ingest@.service \
  /etc/systemd/system/coi-ingest@.service
sudo install -o root -g root -m 0644 \
  /opt/coi/current/deploy/systemd/coi-ingest@.timer \
  /etc/systemd/system/coi-ingest@.timer
sudo install -o root -g root -m 0644 \
  /opt/coi/current/deploy/logrotate/coi /etc/logrotate.d/coi
sudo systemd-analyze verify /etc/systemd/system/coi-ingest@.service \
  /etc/systemd/system/coi-ingest@.timer
sudo logrotate --debug /etc/logrotate.d/coi
sudo systemctl daemon-reload
```

Before enabling timers, run the actual database health command under the
protected environment file without printing that environment:

```bash
sudo systemd-run --unit=coi-db-check-manual --wait --pipe --collect \
  --uid=coi --gid=coi --working-directory=/opt/coi/current \
  --property=EnvironmentFile=/etc/coi/coi.env \
  /opt/coi/current/.venv/bin/python -m coi_backend.cli check
```

The expected output starts with `Database OK`. This validates the database and,
when secret-name mode is active, Key Vault/managed identity. It does not test
Blob Storage. Next exercise the installed unit:

```bash
sudo systemctl start coi-ingest@invoice.service
sudo systemctl status coi-ingest@invoice.service --no-pager
sudo journalctl -u coi-ingest@invoice.service --since '-15 minutes' --no-pager
sudo tail -n 100 /var/log/coi/ingestion.log
```

With an empty inbox, the expected result is `No PDFs found`; that early exit does
not touch the database or Blob. A separately approved, non-sensitive controlled
PDF ingestion is what proves managed-identity Blob writes. Review the expected
PDF.co cost first, then verify the database row and both durable raw/parser Blob
objects before continuing. The one-shot has no batch-wide timeout because the
first backlog can exceed an arbitrary service deadline; individual external
requests remain bounded by the application.

Archive, quarantine, backup staging, and logs can still fill the VM disk even
though successful raw/parser evidence is durable in Blob. Before unattended
timers, the owner must approve separate local retention rules for each class:
successful archives may be removed only after database-to-Blob reconciliation;
quarantine may contain the only recoverable input and requires case review;
backup staging may be removed only after checksum/upload verification and a
restore drill. This package ships no automatic deletion timer.

Until a tested Azure Monitor/VM Insights disk alert is approved, put this check
on the daily operator checklist and record its result:

```bash
df -h / /var/lib/coi /var/log/coi
sudo du -x -h --max-depth=2 /var/lib/coi /var/log/coi | sort -h
DISK_USED_PERCENT=$(df --output=pcent /var/lib/coi | tail -n 1 | tr -dc '0-9')
printf 'COI filesystem used: %s%%\n' "$DISK_USED_PERCENT"
```

Set an owner-approved stop threshold based on the actual disk and worst-case
backlog (80% is a conservative starting point, not an automatic policy). At or
above the threshold, stop both timers, investigate, and either expand storage
or execute a separately approved, reconciled retention action. Never delete
quarantine, backups, or Blob evidence merely to clear an alert.

Enable both schedules only after the manual tests pass:

```bash
sudo systemctl enable --now coi-ingest@invoice.timer coi-ingest@oa.timer
systemctl list-timers 'coi-ingest@*'
systemctl show coi-ingest@invoice.service \
  -p User -p Group -p EnvironmentFiles -p ReadWritePaths -p ProtectSystem -p NoNewPrivileges
```

Do not use `systemctl show -p Environment`; it can expose direct secret values to
an authorized observer.

## 10. Operator recovery and paid retry guard

First stop the matching timer, inspect the document, `coi.parse_attempts`, the
retained Blob result, and the known provider job/cost state. Put only the one
approved PDF in a dedicated retry directory because the CLI processes the whole
directory.

`--force-retry` can reclaim an old failed/review record, a stale processing
claim, a retained parser result, or a known provider job. It does not by itself
authorize a new potentially billable PDF.co POST. Run the first recovery tier as
a transient service so systemd reads the protected environment file:

```bash
sudo systemctl stop coi-ingest@invoice.timer
sudo systemd-run --unit=coi-retry-invoice-manual --wait --pipe --collect \
  --uid=coi --gid=coi --working-directory=/opt/coi/current \
  --property=EnvironmentFile=/etc/coi/coi.env \
  /usr/bin/env ARTIFACT_STORE=azure_blob \
  /opt/coi/current/.venv/bin/python -m coi_backend.cli ingest --type invoice \
  --input-dir /var/lib/coi/retry/invoice --force-retry
```

Only after confirming that no retained result or known job can be recovered and
explicitly accepting a new charge may an operator add both flags:

```bash
sudo systemd-run --unit=coi-paid-retry-invoice-manual --wait --pipe --collect \
  --uid=coi --gid=coi --working-directory=/opt/coi/current \
  --property=EnvironmentFile=/etc/coi/coi.env \
  /usr/bin/env ARTIFACT_STORE=azure_blob \
  /opt/coi/current/.venv/bin/python -m coi_backend.cli ingest --type invoice \
  --input-dir /var/lib/coi/retry/invoice \
  --force-retry --allow-new-paid-parse
```

Review the outcome and Blob artifacts, return the source to the normal audited
lifecycle if appropriate, then restart the timer. Use the equivalent `oa` type
and isolated directory for order acknowledgements.

## 11. Backups and restore drills

For the local pilot, take a PostgreSQL custom-format logical backup from a
consistent database state, stage it in `/var/lib/coi/backups` with mode 0600,
and upload it to `db-backups` using managed identity. A timestamp and release ID
belong in the object name. Do not schedule this until an operator has completed
a restore drill.

```bash
set -euo pipefail
STORAGE_ACCOUNT='<main.bicep storageAccountName output>'
BACKUP_UTC=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_RUN_ID=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
RELEASE_ID=$(basename "$(readlink -f /opt/coi/current)")
BACKUP_NAME="coi-${BACKUP_UTC}-${BACKUP_RUN_ID}.dump"
BACKUP_FILE="/var/lib/coi/backups/${BACKUP_NAME}"
BLOB_NAME="postgresql/local-pilot/${BACKUP_UTC}/${RELEASE_ID}/${BACKUP_NAME}"
test -n "$STORAGE_ACCOUNT" && test -n "$RELEASE_ID"
sudo env BACKUP_FILE="$BACKUP_FILE" bash -o pipefail -c \
  'umask 077; set -o noclobber; sudo -u postgres pg_dump --format=custom --no-owner coi > "$BACKUP_FILE"'
sudo env BACKUP_NAME="$BACKUP_NAME" bash -c \
  'cd /var/lib/coi/backups && umask 077 && set -o noclobber && sha256sum "$BACKUP_NAME" > "${BACKUP_NAME}.sha256"'
BACKUP_SHA256=$(sudo awk '{print $1}' "${BACKUP_FILE}.sha256")
sudo az login --identity --allow-no-subscriptions >/dev/null
sudo az storage blob upload --auth-mode login \
  --account-name "$STORAGE_ACCOUNT" --container-name db-backups \
  --file "$BACKUP_FILE" --name "$BLOB_NAME" --overwrite false \
  --metadata release="$RELEASE_ID" sha256="$BACKUP_SHA256"
sudo az storage blob upload --auth-mode login \
  --account-name "$STORAGE_ACCOUNT" --container-name db-backups \
  --file "${BACKUP_FILE}.sha256" --name "${BLOB_NAME}.sha256" --overwrite false
sudo az storage blob show --auth-mode login --account-name "$STORAGE_ACCOUNT" \
  --container-name db-backups --name "$BLOB_NAME" \
  --query '{name:name,size:properties.contentLength,etag:properties.etag,lastModified:properties.lastModified,metadata:metadata}' -o yaml
```

The local shell uses `noclobber`, and Blob upload uses `--overwrite false`, so a
name collision fails instead of replacing evidence. After the first successful
restore drill, implement the same operation as a reviewed nightly timer; this
package intentionally does not schedule an unproven backup job. Azure Storage
encrypts the Blob at rest, but the VM staging copy is still sensitive. Confirm
the existing OS disk encryption posture, restrict the file, and follow the
approved secure-removal procedure after upload verification.

At least quarterly, download a chosen backup into an isolated host/database,
run `bootstrap_roles.sql`, restore with `pg_restore --no-owner`, reapply
`least_privilege.sql`, grant the isolated runtime login, run `coi_backend.cli
check`, and compare row counts and hashes. Never overwrite the active database
during a drill. Record RPO, RTO, backup checksum, restore duration, and reviewer.

One concrete isolated local-PostgreSQL drill is:

```bash
set -euo pipefail
STORAGE_ACCOUNT='<main.bicep storageAccountName output>'
BLOB_NAME='<reviewed db-backups object name ending in .dump>'
RESTORE_DB="coi_restore_$(date -u +%Y%m%dT%H%M%SZ)"
RESTORE_DIR=$(mktemp -d /var/tmp/coi-restore.XXXXXX)
RESTORE_FILE="$RESTORE_DIR/$(basename "$BLOB_NAME")"
az login --identity --allow-no-subscriptions >/dev/null
az storage blob download --auth-mode login --account-name "$STORAGE_ACCOUNT" \
  --container-name db-backups --name "$BLOB_NAME" --file "$RESTORE_FILE" \
  --overwrite false
az storage blob download --auth-mode login --account-name "$STORAGE_ACCOUNT" \
  --container-name db-backups --name "${BLOB_NAME}.sha256" \
  --file "${RESTORE_FILE}.sha256" --overwrite false
(cd "$RESTORE_DIR" && sha256sum --check "$(basename "${RESTORE_FILE}.sha256")")
sudo chgrp -R postgres "$RESTORE_DIR"
sudo chmod 0750 "$RESTORE_DIR"
sudo chmod 0640 "$RESTORE_FILE" "${RESTORE_FILE}.sha256"
sudo -u postgres createdb "$RESTORE_DB"
sudo -u postgres pg_restore --exit-on-error --no-owner --no-privileges \
  --dbname "$RESTORE_DB" "$RESTORE_FILE"
sudo -u postgres psql -d "$RESTORE_DB" -v ON_ERROR_STOP=1 \
  -f /opt/coi/current/sql/grants/bootstrap_roles.sql
sudo -u postgres psql -d "$RESTORE_DB" -v ON_ERROR_STOP=1 \
  -f /opt/coi/current/sql/grants/least_privilege.sql
sudo -u postgres psql -d "$RESTORE_DB" -v ON_ERROR_STOP=1 \
  -c 'GRANT coi_runtime TO coi;'
(cd /opt/coi/current && sudo -u coi env \
  DATABASE_URL="postgresql:///$RESTORE_DB?host=/var/run/postgresql" \
  /opt/coi/current/.venv/bin/python -m coi_backend.cli check)
sudo -u postgres psql -d "$RESTORE_DB" -Atqc \
  "SELECT count(*), md5(coalesce(string_agg(sha256, '' ORDER BY sha256), '')) FROM coi.documents;"
```

Use an approved operator login instead of `az login --identity` when the
isolated host has no managed identity, with Reader access scoped only to the
`db-backups` container. Compare the final count/hash with the recorded source
value. Keep the restored database and staging files until review is signed;
cleanup is a separate explicit action.

When Flexible Server becomes authoritative, its configured backup retention
provides point-in-time recovery, but a restore always creates a new server; it
does not overwrite the source. Keep periodic logical exports for portability.
See [Flexible Server backup and restore](https://learn.microsoft.com/en-us/azure/postgresql/backup-restore/concepts-backup-restore).

## 12. Optional Azure Monitor onboarding

Local log rotation works without Azure Monitor. Deploy
`monitoring.bicep` only after approving Log Analytics ingestion cost and
confirming `/var/log/coi/ingestion.log` contains no secrets or document content.
The template references the existing VM, installs/enables Azure Monitor Agent,
creates `COIIngestion_CL`, and associates a custom text-log DCR. Microsoft
documents the [custom text-log prerequisites and DCR flow](https://learn.microsoft.com/en-us/azure/azure-monitor/vm/data-collection-log-text).

```bash
MONITORING_PARAMETERS=/secure/operator/path/monitoring.parameters.json
az deployment group validate --resource-group "$RG" \
  --template-file infra/azure/monitoring.bicep --parameters @"$MONITORING_PARAMETERS"
az deployment group what-if --resource-group "$RG" --name coi-monitoring-dev-01 \
  --template-file infra/azure/monitoring.bicep --parameters @"$MONITORING_PARAMETERS"
az deployment group create --resource-group "$RG" --name coi-monitoring-dev-01 \
  --template-file infra/azure/monitoring.bicep --parameters @"$MONITORING_PARAMETERS"
```

Allow agent ingestion time, then verify the extension and query without
returning excessive log content:

```bash
az vm extension show --resource-group "$RG" --vm-name "$VM" \
  --name AzureMonitorLinuxAgent \
  --query '{state:provisioningState,version:typeHandlerVersion,autoUpgrade:enableAutomaticUpgrade}' -o yaml
WORKSPACE_GUID=$(az monitor log-analytics workspace show --resource-group "$RG" \
  --workspace-name "$WORKSPACE" --query customerId -o tsv)
az monitor log-analytics query --workspace "$WORKSPACE_GUID" \
  --analytics-query 'COIIngestion_CL | where TimeGenerated > ago(24h) | summarize records=count(), errors=countif(RawData has " ERROR ")' -o table
```

Keep systemd exit status and database/Blob reconciliation as the primary health
signals. Add an action-group-backed scheduled-query alert only after defining an
on-call recipient and testing the query against known success, failure, and
`needs_review` records; otherwise it creates noisy, unactionable paging and
additional charges.

## 13. Later production gate: private Flexible Server

Do not deploy `postgresql.bicep` for the pilot. Before the business-critical or
live-data phase, require all of the following:

- an approved cost/SKU/backup/availability review;
- an empty dedicated subnet delegated only to
  `Microsoft.DBforPostgreSQL/flexibleServers`, at least `/28` and preferably
  larger;
- the VM and delegated subnet in the same region and connected virtual network
  (or an explicitly reviewed peering design);
- NSGs/routes that allow only required private flows, including VM-to-database
  TCP 5432, PostgreSQL subnet service dependencies, DNS, and HTTPS;
- a private DNS zone ending in `.postgres.database.azure.com` linked to the VM's
  VNet;
- separate deployment and runtime logins, approved secret rotation, TLS
  verification, restore test, and cutover/rollback plan.

Microsoft's [Flexible Server private-network guidance](https://learn.microsoft.com/en-us/azure/postgresql/network/concepts-networking-private)
describes the delegated-subnet and private-DNS constraints. Providing those two
resource IDs selects private VNet integration; Azure supports the
`publicNetworkAccess` property only for servers that are not integrated into a
customer VNet. The template therefore omits that property and creates no
firewall rule or public endpoint. It uses the server FQDN; never pin its private
IP.

Register the later providers and inspect regional SKU availability first:

```bash
az provider register --namespace Microsoft.DBforPostgreSQL
az provider register --namespace Microsoft.Network
az postgres flexible-server list-skus --location "$VM_LOCATION" -o table
az network vnet subnet show --ids '<delegated subnet resource id>' \
  --query '{id:id,prefix:addressPrefix,delegations:delegations[].serviceName,nsg:networkSecurityGroup.id,routeTable:routeTable.id}' -o yaml
```

Copy `postgresql.parameters.example.json` outside the repository and replace all
sentinels. It intentionally omits `administratorLoginPassword`; supply that
secure Bicep parameter from an approved deployment pipeline or a mode-0600
uncommitted parameter file, never a command-line literal or shell-history entry.
Explicitly review `highAvailabilityMode` (`Disabled`, `SameZone`, or
`ZoneRedundant`) and `geoRedundantBackup` (`Disabled` or `Enabled`) against the
chosen region/SKU, recovery objectives, and quote. Defaults remain disabled for
cost control; a production approval must either enable the supported controls
or record acceptance of the single-zone/non-geo risk. Then run `validate` and
`what-if`. The `create` below is a billable production action and requires
explicit approval:

```bash
POSTGRES_PARAMETERS=/secure/operator/path/postgresql.parameters.json
POSTGRES_SECRET_PARAMETERS=/secure/operator/path/postgresql.secure.parameters.json
az deployment group validate --resource-group "$RG" \
  --template-file infra/azure/postgresql.bicep \
  --parameters @"$POSTGRES_PARAMETERS" @"$POSTGRES_SECRET_PARAMETERS"
az deployment group what-if --resource-group "$RG" --name coi-postgresql-prod-01 \
  --template-file infra/azure/postgresql.bicep \
  --parameters @"$POSTGRES_PARAMETERS" @"$POSTGRES_SECRET_PARAMETERS"
az deployment group create --resource-group "$RG" --name coi-postgresql-prod-01 \
  --template-file infra/azure/postgresql.bicep \
  --parameters @"$POSTGRES_PARAMETERS" @"$POSTGRES_SECRET_PARAMETERS"
```

Create distinct application login principals in an ephemeral administrative
session. As database administrator, run `bootstrap_roles.sql`, grant
`coi_migrator` to the deployment login, migrate with
`DATABASE_MIGRATION_ROLE=coi_owner`, run `least_privilege.sql` as administrator,
then grant only `coi_runtime` to the runtime login. Store only the runtime DSN in
`coi-database-url`; the service must never use the server administrator or
deployment credential.

The runtime DSN must use the Azure FQDN and `sslmode=verify-full` with the
Microsoft-documented trust chain. Azure enforces TLS, but client defaults are
not uniformly certificate-verifying; follow Microsoft's [PostgreSQL TLS
connection guidance](https://learn.microsoft.com/en-us/azure/postgresql/security/security-tls-how-to-connect).

Validate from the VM before cutover:

```bash
az postgres flexible-server show --resource-group "$RG" --name '<server name>' \
  --query '{state:state,version:version,fqdn:fullyQualifiedDomainName,subnet:network.delegatedSubnetResourceId,dns:network.privateDnsZoneArmResourceId,backupDays:backup.backupRetentionDays,geoBackup:backup.geoRedundantBackup,ha:highAvailability.mode}' -o yaml
az postgres flexible-server firewall-rule list --resource-group "$RG" \
  --server-name '<server name>' -o table
getent ahostsv4 '<server fqdn>'
nc -vz '<server fqdn>' 5432
```

Name resolution must return a private address from the VM; no public firewall
rules should exist. Run migration/check against a staging database first, take a
recovery point, stop both timers, cut over the Key Vault runtime secret, run the
health check, process one controlled document, and only then re-enable timers.

## 14. Release rollback and resource retirement

Application rollback is one atomic symlink change to a previously retained
release bundle (code plus its `.venv`), followed by service, timer, and logrotate
policy reinstall, `daemon-reload`, a health check, and timer restart.
Do not roll application code backward across an incompatible forward-only
database migration; use a reviewed forward repair or restore the database into
a new isolated target.

```bash
set -euo pipefail
sudo systemctl stop coi-ingest@invoice.timer coi-ingest@oa.timer
while systemctl is-active --quiet coi-ingest@invoice.service \
  || systemctl is-active --quiet coi-ingest@oa.service; do
  printf 'Waiting for active ingestion to finish...\n'
  sleep 5
done
sudo ln -sfnT '/opt/coi/releases/<previous release id>' /opt/coi/current
sudo install -o root -g root -m 0644 \
  /opt/coi/current/deploy/systemd/coi-ingest@.service \
  /etc/systemd/system/coi-ingest@.service
sudo install -o root -g root -m 0644 \
  /opt/coi/current/deploy/systemd/coi-ingest@.timer \
  /etc/systemd/system/coi-ingest@.timer
sudo install -o root -g root -m 0644 \
  /opt/coi/current/deploy/logrotate/coi /etc/logrotate.d/coi
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/coi-ingest@.service \
  /etc/systemd/system/coi-ingest@.timer
sudo logrotate --debug /etc/logrotate.d/coi
if sudo systemd-run --unit=coi-db-check-rollback --wait --pipe --collect \
  --uid=coi --gid=coi --working-directory=/opt/coi/current \
  --property=EnvironmentFile=/etc/coi/coi.env \
  /opt/coi/current/.venv/bin/python -m coi_backend.cli check; then
  sudo systemctl start coi-ingest@invoice.timer coi-ingest@oa.timer
else
  printf 'Rollback health check failed; timers remain stopped.\n' >&2
  exit 1
fi
```

For resource retirement, first stop ingestion, verify and restore-test exported
data, document retention approval, inventory role assignments/private DNS/DCR
associations, and obtain owner authorization. Delete locks are intentional:
remove only the exact approved lock immediately before deleting its exact
resource, and record that change. Never delete the whole resource group as a
shortcut. Purge protection on Key Vault cannot be disabled, and a deleted vault
name remains reserved for its retention period.

## Acceptance checklist

- Account, tenant, region, existing VM, NIC/NSG, and restricted SSH path are recorded.
- Every Bicep file builds; account-specific `validate` and `what-if` are reviewed.
- Budget contacts are real and alerts are acknowledged as non-enforcing.
- Storage settings match the expected secure values and exactly four containers exist.
- VM identity has container-scoped Blob access; no key, connection string, or SAS is configured.
- Protected environment file is `root:root` mode `0600`, or Key Vault secret-name mode is verified.
- PostgreSQL 16 roles/migrations/grants were applied in the documented order.
- Runtime health passes with no migration role; 5432 and application ports are not public.
- Systemd hardening verifies, both log streams reach the protected log, and logrotate parses.
- Local retention is owner-approved; daily disk checks or a tested disk alert are active.
- One controlled document has database provenance and durable raw/parser Blob artifacts.
- A Blob-hosted database backup has been restored into an isolated database.
- Optional Azure Monitor ingestion and later Flexible Server remain explicit, costed gates.

Resource naming follows Microsoft's [Cloud Adoption Framework naming guidance](https://learn.microsoft.com/en-us/azure/cloud-adoption-framework/ready/azure-best-practices/resource-naming)
and [recommended abbreviations](https://learn.microsoft.com/en-us/azure/cloud-adoption-framework/ready/azure-best-practices/resource-abbreviations).
