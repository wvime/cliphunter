# Migración editorial

La base se actualiza sola en el siguiente `run` o `init-db`. No hay un paso manual.

`cliphunter/storage/schema.sql` crea, si no existen:

- `participants`, `participant_nicknames`
- `relationships`
- `storylines`
- `publish_results`

Una base ya creada no altera una tabla vieja con `CREATE TABLE IF NOT EXISTS`. Las columnas nuevas de `decisions` entran por `_COLUMN_PATCHES` en `cliphunter/storage/repo.py`:

- `decisions.reason`
- `decisions.category`
- `decisions.participants_json`

`verified = 0` es un rumor. `verified = 1` es un dato con fuente. El import deja rumor salvo que el JSON diga `verificado: true`.

La cola de revisión no cambia de contrato para un clip que llegue de fuera: `status=pending` y los códigos `a`, `r`, `is`, `ie`, `fs`, `fe` siguen valiendo. `r` rechaza en el acto. En Telegram, el botón Rechazar manda `ask` y después `why:<id>:<motivo>`.
