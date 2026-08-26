-- Bootstrap the fixed, password-free group roles and owned application schema.
--
-- Fresh database order:
--   1. Run this file as the database administrator.
--   2. As the administrator, grant coi_migrator to the real deployment login.
--   3. Run migrations with DATABASE_MIGRATION_ROLE=coi_owner.
--   4. Run sql/grants/least_privilege.sql as the administrator.
--   5. Grant coi_runtime to the real application login.
--
-- Example membership statements (replace the quoted placeholders; do not add
-- credentials to this repository):
--   GRANT coi_migrator TO "deployment_login";
--   GRANT coi_runtime TO "application_login";
--
-- Never grant coi_owner or coi_migrator to an application/runtime login. These
-- group roles are NOLOGIN by design; authentication belongs to separately
-- provisioned local, password, or Microsoft Entra-authenticated login roles.

BEGIN;

DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'coi_owner') THEN
        CREATE ROLE coi_owner;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'coi_migrator') THEN
        CREATE ROLE coi_migrator;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'coi_runtime') THEN
        CREATE ROLE coi_runtime;
    END IF;
END;
$roles$;

-- CREATEROLE administrators (including an Azure PostgreSQL administrator)
-- cannot execute an
-- ALTER ROLE statement that mentions SUPERUSER, REPLICATION, or BYPASSRLS,
-- even when those attributes are being removed. CREATE ROLE defaults all three
-- to false. Reapplication restores the attributes an ordinary role
-- administrator may change, then verifies the superuser-only attributes.
ALTER ROLE coi_owner
    NOLOGIN NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE coi_migrator
    NOLOGIN NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE coi_runtime
    NOLOGIN NOCREATEDB NOCREATEROLE INHERIT;

DO $safe_attributes$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname IN ('coi_owner', 'coi_migrator', 'coi_runtime')
          AND (rolsuper OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION
            'a COI group role has a superuser-only attribute; remove it with a true PostgreSQL superuser';
    END IF;
END;
$safe_attributes$;

-- PostgreSQL 16 stores inheritance on each membership. Spell out both options
-- so reapplication also hardens memberships created by an older revision:
-- deployment sessions may SET ROLE through coi_migrator, but never inherit the
-- owner's privileges before the migration runner explicitly assumes it.
GRANT coi_owner TO coi_migrator WITH INHERIT FALSE, SET TRUE;

-- Object ownership changes require membership in the target owner role. A
-- PostgreSQL CREATEROLE administrator can grant roles it administers but is not
-- automatically their member. Record and add only the membership needed by
-- this transaction; preserve a pre-existing membership on reapplication.
CREATE TEMPORARY TABLE pg_temp.coi_bootstrap_actor (
    role_name        NAME NOT NULL,
    membership_added BOOLEAN NOT NULL
) ON COMMIT DROP;

INSERT INTO pg_temp.coi_bootstrap_actor (role_name, membership_added)
SELECT current_user, NOT pg_has_role(current_user, 'coi_owner', 'SET');

DO $temporary_owner_membership$
DECLARE
    actor NAME;
    should_grant BOOLEAN;
BEGIN
    SELECT role_name, membership_added
    INTO actor, should_grant
    FROM pg_temp.coi_bootstrap_actor;

    IF should_grant THEN
        EXECUTE format('GRANT coi_owner TO %I', actor);
    END IF;
END;
$temporary_owner_membership$;

-- Creating the schema here lets coi_owner run the migration bootstrap without
-- involving the administrator in object ownership. The runner also executes
-- CREATE SCHEMA IF NOT EXISTS on every migration check, so its owner role needs
-- database-level CREATE. Only deployment principals can assume this role;
-- coi_runtime never receives it. Reapplying is harmless.
DO $database_grant$
BEGIN
    EXECUTE format(
        'GRANT CONNECT, CREATE ON DATABASE %I TO coi_owner',
        current_database()
    );
END;
$database_grant$;

CREATE SCHEMA IF NOT EXISTS coi AUTHORIZATION coi_owner;
ALTER SCHEMA coi OWNER TO coi_owner;
REVOKE ALL ON SCHEMA coi FROM PUBLIC;

DO $remove_temporary_owner_membership$
DECLARE
    actor NAME;
    should_revoke BOOLEAN;
BEGIN
    SELECT role_name, membership_added
    INTO actor, should_revoke
    FROM pg_temp.coi_bootstrap_actor;

    IF should_revoke THEN
        EXECUTE format('REVOKE coi_owner FROM %I', actor);
    END IF;
END;
$remove_temporary_owner_membership$;

COMMIT;
