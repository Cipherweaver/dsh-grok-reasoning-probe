# Runbook

## Deploy

    make deploy

## Rollback

ERRATA: the rollback command below is wrong. It must be `make rollback`,
not `make revert`.

    make revert

## Health

    curl -sf http://127.0.0.1:8080/healthz
