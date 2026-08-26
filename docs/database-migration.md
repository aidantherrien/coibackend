# Database migration and recovery runbook

## New production database

Use PostgreSQL 16 and two distinct login principals: one for deployment and one
for the running application. The fixed `coi_*` roles are password-free group
roles. This model works on the VM-local pilot and with the non-superuser
administrator supplied by Azure Database for PostgreSQL Flexible Server.

As a database administrator:

```bash
psql "$ADMIN_DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f sql/grants/bootstrap_roles.sql
```

Then grant membership using quoted, account-specific login names:

```sql
GRANT coi_migrator TO "deployment_login";
```

As the deployment login, set `DATABASE_MIGRATION_ROLE=coi_owner` and run:

```bash
python -m coi_backend.cli migrate
```

As administrator, finish grants and then attach the runtime role:

```bash
psql "$ADMIN_DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f sql/grants/least_privilege.sql
```

```sql
GRANT coi_runtime TO "application_login";
```

Remove the migration-role setting from the runtime environment. With the
application login's database secret active, verify:

```bash
python -m coi_backend.cli check
```

The bootstrap and grant scripts are idempotent. Keep the administrator URL out
of shell history and repository files; in production, prefer an ephemeral
administrative session and retrieve credentials from the approved secret store.

## Existing database made by the legacy script

The old schema used unversioned tables in `public` and did not retain enough
provenance to populate `coi.documents` safely. In particular, a trustworthy
SHA-256, raw-artifact location, source occurrence, and parse-attempt history
cannot be reconstructed from normalized rows alone.

For that reason, the migration runner deliberately refuses to silently
baseline a database containing `public.invoice_summary` without the migration
ledger. Do not rename old tables or manually insert fake ledger rows to bypass
that check.

The safest transition is a side-by-side rebuild:

1. Stop legacy ingestion and record the cutoff time.
2. Create and verify a recoverable backup of the old database.
3. Provision a new empty PostgreSQL 16 database.
4. Apply the role bootstrap, migrations, and least-privilege grants above.
5. Reingest the retained original PDFs. This computes real hashes and produces
   complete document/attempt provenance.
6. Compare old summary counts/totals with the new database by vendor, document
   number, PO, and currency. Investigate review/conflict rows rather than
   force-inserting them.
7. Keep the old database read-only for the agreed retention window, then remove
   it only after a restore test and owner approval.

A custom one-time ETL can preserve legacy normalized rows when original PDFs no
longer exist, but it must explicitly label synthetic provenance in metadata and
cannot provide the same audit guarantee. Build and review that migration as a
new, separately tested deliverable rather than changing `0001_initial_schema.sql`.

## Backups and release changes

Before every production migration:

- confirm the application package and migration directory are from the same
  immutable release;
- take an Azure Flexible Server recovery point or a verified `pg_dump` according
  to the recovery policy (for the VM-local pilot, copy the encrypted dump to the
  private `db-backups` Blob container);
- inspect pending migration versions in a staging copy;
- stop ingestion timers so application writes do not race the change;
- apply migrations with `ON_ERROR_STOP`/the migration CLI and run the health
  check as the runtime login;
- restart timers and review Azure Monitor and systemd logs.

Migrations are forward-only. Never edit an applied migration: add the next
numbered file. Application rollback is safe only when the older application is
compatible with the newer schema. For an incompatible migration, prepare and
test an explicit forward repair or restore the whole database to a new instance
from the pre-change snapshot.

At least quarterly, restore a backup into an isolated database, run
`coi-backend check`, compare row counts and hashes, and record the achieved
recovery time. A backup that has never been restored is not a verified backup.
