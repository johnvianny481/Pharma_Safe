#!/bin/bash
# Local development only. Creates the application's login role. It is a member of the
# NOLOGIN group role pharmasafe_app, which holds the table privileges granted by
# migrations. It is not the owner and has no BYPASSRLS, so row-level security applies.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname pharmasafe \
     -v app_password="$PHARMASAFE_APP_DB_PASSWORD" <<'EOSQL'
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pharmasafe_app') THEN
        CREATE ROLE pharmasafe_app NOLOGIN;
    END IF;
END $$;
CREATE ROLE pharmasafe_app_user LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD :'app_password'
    IN ROLE pharmasafe_app;
EOSQL
