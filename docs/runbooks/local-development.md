# Runbook: local development

## Start and stop

| Task | make | Windows (no make) |
|---|---|---|
| Install deps | `make install` | `./scripts/dev.ps1 install` |
| Start stack (waits for healthy) | `make up` | `./scripts/dev.ps1 up` |
| Status | `make ps` | `./scripts/dev.ps1 ps` |
| Logs | `make logs` | `./scripts/dev.ps1 logs` |
| Stop (keep data) | `make down` | `./scripts/dev.ps1 down` |
| Stop and wipe volumes | `make reset` | `./scripts/dev.ps1 reset` |
| Everything CI runs | `make ci` | `./scripts/dev.ps1 ci` |

## Troubleshooting

- **`up` times out on keycloak or langfuse.** On a slow first start, Keycloak runs a build step before it serves traffic. Run `make up` again; it picks up where it left off. If it still fails, check `docker compose -f deploy/docker-compose.yml logs keycloak`.
- **Port already in use.** Override the port in `.env` (for example `POSTGRES_PORT=5433`).
- **Changed `deploy/postgres/init/*.sql` but nothing happened.** Init scripts only run on an empty volume. Run `make reset` and then `make up`.
- **`.env` missing variable error from compose.** Compare `.env` with `.env.example` and add the missing key.

## Credentials

Every local credential lives in `.env`, which is created from `.env.example` and never committed. The values are for local use only. Production secrets live in Azure Key Vault.
