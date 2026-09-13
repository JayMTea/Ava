# Remote GPU memory inventory

Ava can monitor another host through its existing node_exporter and GPU exporter.
Run `ava_bridge/gpu_inventory.py` on that host to add GPU process/model inventory
to the same node_exporter endpoint. It uses only Python's standard library and
`nvidia-smi`; it never loads, unloads or interrupts a model.

The collector reads NVIDIA compute-process allocations and host `/proc` metadata.
Workers are grouped with the parent that declares their model. Mapped/open model
files identify components; these do not establish each component's GPU residency
or memory size. Process allocation includes weights, caches and runtime overhead.
Only runtime names, model identities, component basenames, PIDs and allocation
sizes are exported. Commands, environment variables and absolute paths are omitted.

## Installation

Copy the standalone module to `~/.local/lib/ava/gpu_inventory.py`. Set this variable in
`~/.config/ava/gpu-inventory.env`, choosing the directory already configured on your
node_exporter:

```dotenv
AVA_GPU_INVENTORY_OUTPUT=/var/lib/node_exporter/textfile/ava_gpu_inventory.prom
```

Install `deploy/gpu-inventory.service` as `~/.config/systemd/user/ava-gpu-inventory.service`
and enable it with `systemctl --user enable --now ava-gpu-inventory`. The account needs
permission to write the textfile directory and read GPU processes' `/proc` metadata.
Enable systemd lingering if it must run without a login session. It refreshes the file every
two seconds; it opens no additional network endpoint. Protect the existing exporter
endpoint with the same private-network controls as other host metrics.

For a foreground smoke check:

```sh
python3 ~/.local/lib/ava/gpu_inventory.py --output /var/lib/node_exporter/textfile/ava_gpu_inventory.prom --once
```

Set the existing DCGM exporter container's `DCGM_EXPORTER_INTERVAL=1000` for
one-second utilization collection. Recreate only that telemetry container,
preserving its image, GPU access, mounts and listener configuration. Its default
30-second sampling can miss a short inference. Ava refreshes the open monitor
every second and the collapsed bubble every two seconds.

## Interpretation and failure behavior

The collector reports success and observation time even for an empty inventory.
Ava ignores failed snapshots or snapshots older than 15 seconds and explicitly
reports unavailable inventory. It never substitutes the bridge host's processes
for those of the monitored host. Model rows are attributed to the brain only when
both the configured agent/backend hostname and the exact observed model identity
match. Ambiguous matches remain unattributed. Host aliases need consistent
configuration; an unrelated machine with the same model name cannot claim the row.

On unified-memory NVIDIA hardware, the memory percentage is whole-system memory
occupancy, not GPU compute utilization. The process rows explain observed GPU allocations; they
are not a complete accounting of OS, CPU processes, cache or shared pages.

## Deployment validation (2026-09-13)

These figures were measured on a GB10 with one inference runtime and ComfyUI;
expect different figures with other models, runtime settings or hardware.

- Targeted backend coverage: 128 tests and 43 subtests passed, including process
  grouping, privacy, stale/missing telemetry and host/model attribution.
- Whole-app backend QA: 114 passed, 3 skipped. Frontend: 433 tests passed;
  TypeScript and the production build passed. Changed-file frontend lint passed
  with one existing array-index-key warning.
- A bounded 512-token inference produced a utilization rise within about one
  second; DCGM followed NVIDIA's samples and returned to zero after completion.
  The deployed browser recorded 43 hardware responses, a peak of 86%, and no
  JavaScript errors.
- Browser checks confirmed the brain's measured allocation (~44 GiB), ComfyUI's
  allocation (~65 GiB) and its three identified component files. Light/dark
  screenshots and a 390-pixel mobile viewport check passed.
- Both telemetry services are active with DCGM's interval set to 1000 ms. The
  existing inference and ComfyUI process IDs were unchanged after deployment.

Local validation artifacts are in `logs/gpu-monitor-validation/` (screenshots and
`results.json`). Backend logs are `logs/gpu-inventory-tests.log` and
`logs/gpu-inventory-qa.log`. These generated files are intentionally untracked.
