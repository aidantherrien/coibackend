# Security notes

## Secrets and private data

Never commit database credentials, PDF.co keys, Azure service-principal secrets,
storage account keys or connection strings, SAS tokens, `.env` files, database
dumps, SSH/private keys, source PDFs, raw emails, or parser results. The Azure VM
uses its system-assigned managed identity for Blob Storage and Key Vault. No
long-lived Azure credential belongs on the VM or in this repository.

For the controlled pilot, direct `DATABASE_URL` and `PDFCO_API_KEY` values may be
stored in `/etc/coi/coi.env`, owned by root and mode `0600`. Once Key Vault
RBAC is available, set only the vault URL and secret names. Direct values and
their Key Vault alternatives are mutually exclusive by design.

The live source tree contains no intentional credential values. A credential
was present in an earlier Git commit before this cleanup. Removing it from the
current file does not revoke it or erase Git history. Before publication or
deployment:

1. Rotate or revoke that database credential if it was ever usable.
2. Confirm dependent machines and scheduled tasks use the replacement.
3. Run the repository's full-history secret scan.
4. If policy requires historical removal, coordinate a `git-filter-repo` or
   equivalent rewrite with every clone owner, then force-push only after backups
   and explicit approval. Normal setup and deployment scripts intentionally do
   not rewrite Git history.

`.gitleaksignore` contains only exact fingerprints for the known legacy
findings, so CI can still reject every new occurrence. It is not a path or rule
exemption.

## Azure access

- Keep the repository private and use each person's own Microsoft Entra
  identity. Do not share an Owner account or SSH private key.
- The VM identity needs only `Storage Blob Data Contributor` on each required
  application container and `Key Vault Secrets User` on the application vault.
- Creating resources as Contributor does not imply permission to assign Azure
  roles. An Owner, User Access Administrator, or appropriately delegated role
  must review data-plane assignments.
- Do not grant anonymous Blob access. Do not configure the application with a
  storage account key merely to bypass RBAC.
- Keep PostgreSQL and application ports closed to the public internet. The
  pilot uses restricted SSH; a private search process should bind to loopback
  and be reached through an SSH tunnel.
- Keep Key Vault purge protection and soft delete enabled. Blob versioning,
  blob/container soft delete, and tested backups are complementary controls;
  none is a substitute for a restore drill.

## Reporting

Treat a suspected leaked secret, exposed Blob, overly broad Azure RBAC role,
unapproved network rule, or unauthorized database access as an incident. Remove
or revoke the exposure first, preserve relevant Azure Activity, Key Vault,
Storage, Azure Monitor, systemd, and PostgreSQL evidence, and notify the
repository and Azure subscription owners through the private company incident
channel. Never open a public issue containing credentials or customer
documents.
