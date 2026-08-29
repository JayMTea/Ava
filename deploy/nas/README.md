# Ava on the NAS: the shipper sidecar

This directory holds the pieces of Ava's NAS deployment that are committed here rather than
living only on the NAS. Today that is the **shipper sidecar**: the `datastack-shipper` instance
that reads Ava's state read-only and feeds the lab's medallion (bronze objects in MinIO, raw rows
in ClickHouse). Ava itself is not changed by any of it — no client, no credential, no outbound
dependency; `perf_store.py` and the in-app dashboard are untouched.

| File | What it is |
|---|---|
| `shipper.yaml` | Which files leave `/volume1/apps/ava`, to which bucket prefix and which `raw_ava` table. Declarative; `datastack-shipper --describe` prints it. |
| `shipper.compose.yml` | A compose **fragment** adding the `ava-shipper` service to the `ava` project. |
| `test_shipper_config.py` | Validates `shipper.yaml` through the shipper's own loader and runs its streams against the shipper's fakes. Skips when `datastack.shipper` is not importable (i.e. in Ava's own test run); run it from the home-lab data-stack checkout. |

The warehouse side — the `raw_ava` table definitions, the silver/gold SQL — is the connector
pack at `home-lab/data-stack/connectors/ava/`. The two are a handshake: every `target.table`
here is an asset there, and `source_name: ava-bridge` here is the pack's manifest name, which is
how the Dagster watermark sensor on the Spark finds these rows.

## How the operator merges it

The NAS overlay (`/volume1/stacks/ava/src/deploy/docker-compose.override.yml`) is merged by
name on every `docker compose` call. This fragment is not — it is named explicitly, and only on
the NAS:

```bash
cd /volume1/stacks/ava/src/deploy
docker compose -f docker-compose.yml -f docker-compose.override.yml -f nas/shipper.compose.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.override.yml -f nas/shipper.compose.yml up -d ava-shipper
```

Every later `docker compose` command that should see the shipper (`ps`, `logs`, `down`) needs
the same three `-f` flags. Setting `COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:nas/shipper.compose.yml`
in `deploy/.env` makes that the default for the project, which is the simplest way to keep
`down` from forgetting a service that `up` started.

The container name compose assigns is `ava-ava-shipper-1` (project `ava`, service
`ava-shipper`); that is the name for the observability `ContainerAbsent` row.

### Before the first `up`

1. **Image.** Built on the NAS from the home-lab checkout (`/volume1/stacks/datastack-src`
   holds the data-stack tree; adjust the path if it differs):
   ```bash
   cd /volume1/stacks/datastack-src/data-stack
   docker build -f deployment/docker/shipper.Dockerfile -t datastack-shipper:0.1.0 .
   ```
2. **Credentials**, by NAME in `shipper.yaml`, by VALUE in `deploy/.env` (LF line endings,
   hand-written — the same rule as the rest of Ava's `.env` on this box):
   ```
   MINIO_AVA_ACCESS_KEY=ava-app
   MINIO_AVA_SECRET=<MINIO_AVA_SECRET from platform/.env>
   DATASTACK_CH_AVA_INGESTOR_PASSWORD=<DATASTACK_CH_AVA_INGESTOR_PASSWORD from data-stack/.env>
   ```
   No other value from `deploy/.env` reaches the shipper: the fragment passes exactly these
   three by name and declares no `env_file`.
3. **State dir.** `/volume1/apps/ava-shipper` exists, `chattr +C`, owned `1000:10`
   (`platform/scripts/host-prep.sh` step 6 creates it).
4. **Networks.** `datastack_warehouse` (data-stack's warehouse compose) and `minio_s3`
   (platform's MinIO stack) exist: `docker network ls`. The fragment declares both `external`
   and will refuse to start without them.
5. **Warehouse.** `raw_ava` exists (migration `0009`), the `ava_ingestor` role exists
   (`make -C data-stack warehouse-bootstrap`), and the raw tables exist
   (`make -C data-stack register`, which renders them from the pack's manifest). The shipper
   inserts; it does not create tables.

### Verify

```bash
docker compose ... exec ava-shipper datastack-shipper --describe
docker compose ... exec ava-shipper datastack-shipper --once --stream perf
docker compose ... logs -f ava-shipper
```

Then, from the dev box, `SELECT count() FROM raw_ava.llm_generation` as `ava_reader`, and
`mc ls nas/ava-bronze/perf/`. A `docker restart` of the container must not change either count:
every slice carries an `insert_deduplication_token` and the state file is only advanced after
both writes succeed.

## What ships, and what does not

- **Ledgers, as-is**: `performance.jsonl` (+ rotations), `audit.jsonl` (chain verified nightly
  over the bronze copy), `kpi/ledger.jsonl`, `kpi/definitions.jsonl`, `alloc.jsonl`,
  `hw_history/hw_1m.jsonl`, `hw_history/hw_1h.jsonl`, `devices/<cid>.jsonl`. One row per line
  into the matching `raw_ava` table; the line itself is the bronze object.
- **Nightly SQLite snapshots** of `data/chats.db` and `data/memory.db` to `ava-bronze/sqlite/`:
  copy + `wal_checkpoint(TRUNCATE)` + `quick_check` in the shipper's own scratch, never the
  live file on the read-only mount. Bronze only — a backup, not a table.
- **`chat_message` — declared, `enabled: false`.** The privacy decision (plan open question 2):
  chat rows do not become warehouse rows unless the owner turns this on. It is listed rather
  than omitted so `--describe` shows the decision.
- **`media_uploads` — declared, `enabled: false`.** The engine grew a `file_tree` kind
  (`datastack.shipper.file_tree`), so `media/uploads` is now a declared stream rather than an
  operator's `mc mirror`: one object per file, byte-for-byte, content type from the extension,
  `x-amz-meta-sha256` on every object, keyed by content at
  `ava-bronze/media/<sha[:2]>/<sha><ext>`. Bronze only — a JPEG is not rows, and the engine
  refuses a `target` on this kind; the inventory is `meta.landing_objects`, one row per object.
  It is off because turning it on is the operator's call. Before flipping `enabled: true`,
  check the tree is readable by uid 1000 (see the constraints below), `du -sh` it, and remember
  `ava-bronze` is versioned with **no** Delete. The first pass reads at most
  `max_files_per_tick` (200) files per tick — the engine polls streams in turn on one thread, so
  an unbounded first pass would stall `perf` and `audit` behind it; a large tree simply takes
  several ticks, and the steady state afterwards is one `stat` per file.

## Constraints on this box (restated from the plan)

- No `install.sh`; the `.env` is hand-written with LF endings.
- `AVA_MODEL` is verbatim `nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4`;
  `AVA_BACKEND_URL=http://100.120.254.9:8002/v1` (the Spark, over the tailnet).
- The shipper's uid is `1000:10`. The `ava` container runs as root today, so
  `logs/audit.jsonl`, `data/chats.db`, `data/memory.db` and (once it exists) `logs/kpi/` are
  `0600 root` on the host and the shipper cannot read them: those streams will report
  `PermissionError` and the container turns unhealthy after three ticks. `performance.jsonl`
  and `hw_history/` are `0644` and ship regardless. Resolution is Ava's own overlay (run the
  `ava` service as `1000:10`, with a one-time `chown -R 1000:10 /volume1/apps/ava` — needs
  sudo) or a group-readable mode on those files; either is a change to the live Ava overlay,
  which is the operator's call, not this fragment's.
