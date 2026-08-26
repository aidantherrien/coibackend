-- DEPRECATED: this former one-shot schema file is intentionally not executable.
--
-- Apply every versioned file in sql/migrations/ in filename order instead.
-- The migration runner wraps each file in an explicit transaction and records
-- its version plus checksum in coi.schema_migrations. Bootstrap roles first
-- with sql/grants/bootstrap_roles.sql, then apply
-- sql/grants/least_privilege.sql after the migrations.
--
-- No psql \i directive is used here because migration execution must work the
-- same way from psql, a Python runner, or a deployment system.

DO $deprecated$
BEGIN
    RAISE EXCEPTION
        'sql/generate_database.sql is deprecated; apply sql/migrations/*.sql in filename order';
END;
$deprecated$;
