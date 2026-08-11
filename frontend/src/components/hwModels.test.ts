// The rules behind the hardware monitor's model list, pinned against the box
// that produced the bug: Ava's brain, a third-party ComfyUI holding 65 GB, another
// app's vLLM, and a backend nobody configured — all four rendered identically in
// one flat dropdown, which read as a list of stale entries belonging to Ava.
import { describe, expect, it } from 'vitest';
import {
  MODEL_RELATION, RELATION_ORDER, foundVia, groupRows, holdsLine, identified,
  relationOf, rowHint, rowTitle,
} from './hwModels';
import type { Row } from './hwModels';

const row = (over: Partial<Row>): Row => ({
  id: 'x', name: 'runtime', model: 'A Model', memory_mb: null, memory_gb: null,
  pid: null, status: 'empty', source: 'api', ...over,
} as Row);

// The four rows the owner actually had on screen.
const BRAIN = row({
  id: 'agent:sandbox', name: 'Agent sandbox', source: 'agent',
  model: 'Reasoner-30B-A3B-FP8',
  state: 'unknown', role_key: 'brain', relation: 'brain', backend: '',
});
const COMFY = row({
  id: 'pid:2325889', name: 'Python runtime', source: 'nvidia-smi',
  model: 'ImageGen · flux2', model_id: null, state: 'resident', status: 'loaded',
  relation: 'foreign', memory_gb: 65.54, pid: 2325889, role_key: '',
  components: [
    { name: 'flux2-vae', kind: 'diffusion', in_memory: true },
    { name: 'flux2_dev_fp8mixed', kind: 'diffusion', in_memory: true },
    { name: 'mistral_3_small_flux2_bf16', kind: 'diffusion', in_memory: true },
  ],
});
const OTHER_VLLM = row({
  id: 'pid:1724971', name: 'vLLM', source: 'nvidia-smi', model: 'Qwen3-8B-AWQ',
  model_id: 'Qwen/Qwen3-8B-AWQ', state: 'resident', status: 'loaded',
  relation: 'foreign', memory_gb: 11.64, role_key: '',
});
const ENV_BACKEND = row({
  id: 'backend:m', name: 'vLLM', source: 'api', model: 'Some Model',
  model_id: 'org/Some-Model', state: 'offline', relation: 'configured',
  backend: 'backend', implicit: true, role_key: '',
});

describe('relationOf', () => {
  it('reads the backend token when the payload carries one', () => {
    expect(relationOf(BRAIN)).toBe('brain');
    expect(relationOf(COMFY)).toBe('foreign');
    expect(relationOf(ENV_BACKEND)).toBe('configured');
  });

  it('never calls a row the brain without role_key, on an old payload', () => {
    // The pre-upgrade fallback must not promote the biggest row, or the
    // heaviest foreign process becomes "Ava's brain".
    const legacy = row({ relation: undefined, memory_gb: 999, role_key: '' });
    expect(relationOf(legacy)).toBe('foreign');
  });

  it('never claims an app without an app id', () => {
    expect(relationOf(row({ relation: undefined, app: '' }))).toBe('foreign');
    expect(relationOf(row({ relation: undefined, app: 'studio' }))).toBe('app');
  });

  it('falls back to configured for a backend-tied row', () => {
    expect(relationOf(row({ relation: undefined, backend: 'b1' }))).toBe('configured');
  });

  it('ignores a token this frontend does not know', () => {
    expect(relationOf(row({ relation: 'sideways' as never }))).toBe('foreign');
  });
});

describe('groupRows', () => {
  const groups = groupRows([BRAIN, COMFY, OTHER_VLLM, ENV_BACKEND]);

  it('splits the four rows into Ava-first sections', () => {
    expect(groups.map((g) => g.relation)).toEqual(['brain', 'configured', 'foreign']);
    expect(groups[0].rows).toEqual([BRAIN]);
    expect(groups[2].rows).toEqual([COMFY, OTHER_VLLM]);
  });

  it('emits no heading for a section with nothing in it', () => {
    // A box with no connected-app engines must not grow an empty
    // "Connected apps" heading.
    expect(groups.some((g) => g.relation === 'app')).toBe(false);
    expect(groupRows([])).toEqual([]);
  });

  it('keeps every relation in the copy table and the order list', () => {
    for (const r of RELATION_ORDER) expect(MODEL_RELATION[r].group).toBeTruthy();
    expect(RELATION_ORDER.length).toBe(Object.keys(MODEL_RELATION).length);
  });
});

describe('naming an unidentified row', () => {
  it('never shows the bare word "Model"', () => {
    // The backend names it from its command line and mapped files; the point
    // of the test is that whatever arrives, the panel shows something specific.
    expect(rowTitle(COMFY)).toBe('ImageGen · flux2');
    expect(identified(COMFY)).toBe(false);
  });

  it('says what an unnamed row is holding', () => {
    expect(holdsLine(COMFY)).toBe('Holds flux2-vae, flux2_dev_fp8mixed +1');
  });

  it('admits when it cannot tell', () => {
    expect(holdsLine(row({ model_id: null, components: [] })))
      .toBe('Ava cannot tell what this program is holding.');
  });

  it('adds no holds-line to a row that was properly identified', () => {
    expect(holdsLine(OTHER_VLLM)).toBe('');
  });

  it('falls back to the runtime when there is no model name at all', () => {
    expect(rowTitle(row({ model: '', name: 'vLLM' }))).toBe('vLLM');
    expect(rowTitle(row({ model: '', name: '' }))).toBe('Unidentified process');
  });
});

describe('rowHint', () => {
  it('never tells the owner a foreign model is "ready to answer"', () => {
    // MODEL_STATE.resident.hint says exactly that, and it is false of another
    // program's image model — it will never answer anything of Ava's.
    expect(rowHint(COMFY)).toBe('');
    expect(rowHint(OTHER_VLLM)).toBe('');
  });

  it('does not report an unconfigured env address as a fault', () => {
    expect(rowHint(ENV_BACKEND)).toContain('AVA_BACKEND_URL');
    expect(rowHint(ENV_BACKEND)).not.toContain('Nothing is answering at its address');
  });

  it('keeps the shared state copy for Ava’s own rows', () => {
    expect(rowHint(BRAIN)).toBe('Ava cannot see inside this runtime to check.');
  });
});

describe('foundVia', () => {
  it('words the source token instead of printing it raw', () => {
    expect(foundVia('nvidia-smi')).toBe('GPU process telemetry');
    expect(foundVia('agent')).toBe("Ava's agent sandbox");
  });

  it('invents nothing for a token it does not know', () => {
    expect(foundVia('some-new-probe')).toBe('some-new-probe');
  });
});
