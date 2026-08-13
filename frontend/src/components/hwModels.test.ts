// The rules behind the hardware monitor's model list, pinned against the box
// that produced the bug: Ava's brain, a third-party ComfyUI holding 65 GB, another
// app's vLLM, and a backend nobody configured — all four rendered identically in
// one flat dropdown, which read as a list of stale entries belonging to Ava.
import { describe, expect, it } from 'vitest';
import {
  MODEL_RELATION, RELATION_ORDER, activityTone, componentMeta, foundVia,
  groupMemoryGb, groupRows, heldGb, holdsLine, identified,
  isAvas, listHint, memPhrase, needsGroupHeads, poolOf, relationOf, rowHint,
  rowSub, rowTitle, servedLine, shareOf, tempTone,
} from './hwModels';
import type { MemPool, Row } from './hwModels';

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
  id: 'pid:1724971', name: 'vLLM', source: 'nvidia-smi', model: 'Mistral-7B-AWQ',
  model_id: 'mistralai/Mistral-7B-AWQ', state: 'resident', status: 'loaded',
  relation: 'foreign', memory_gb: 11.64, role_key: '',
});
const ENV_BACKEND = row({
  id: 'backend:m', name: 'vLLM', source: 'api', model: 'Some Model',
  model_id: 'org/Some-Model', state: 'offline', relation: 'configured',
  backend: 'backend', implicit: true, role_key: '',
});

const APP_VLLM = row({
  id: 'pid:1811', name: 'vLLM', source: 'nvidia-smi', model: 'Pace-Small-3B',
  model_id: 'org/Pace-Small-3B', state: 'resident', status: 'loaded',
  relation: 'app', app: 'pace', memory_gb: 3.2, role_key: '',
});

// The box the panel is designed against: one unified pool, 121.7 GB.
const SPARK: MemPool = poolOf({
  gpu: { name: null, util: null, temp: null, power: null, mem_used_mb: null, mem_total_mb: 124620 },
  mem: { total_gb: 121.7, used_gb: 81.8, free_gb: 39.9, used_pct: 67 },
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

describe('an "In memory" that was concluded, not read', () => {
  // vLLM, llama.cpp and MLX expose no residency endpoint: they load one model at
  // boot and hold it, so being served IS being resident. Correct — and not an
  // observation. The resident hint reads as a measurement either way, and the
  // whole point of the state vocabulary is that liveness is observed.
  it('says how it knows', () => {
    expect(rowHint(row({ backend: 'local', state: 'resident', state_measured: false })))
      .toContain('cannot read its memory directly');
  });

  it('leaves a measured residency alone', () => {
    expect(rowHint(row({ backend: 'local', state: 'resident', state_measured: true })))
      .not.toContain('cannot read its memory directly');
  });

  it('treats an absent flag as measured, so older payloads do not change', () => {
    expect(rowHint(row({ backend: 'local', state: 'resident' })))
      .not.toContain('cannot read its memory directly');
  });
});

describe('the agent sandbox when it is not running', () => {
  // The row used to be hardcoded to `unknown` — the right word for "we cannot
  // see the weights inside it", the wrong one for "the container is stopped".
  // Now that the backend observes liveness, the copy has to match: there is no
  // address here to have nothing answering at, and the reasons differ.
  it('names the sandbox rather than an address', () => {
    const hint = rowHint(row({ ...BRAIN, state: 'offline' }));
    expect(hint).toContain('agent sandbox');
    expect(hint).not.toContain('address');
  });

  it('passes through what the probe found out', () => {
    expect(rowHint(row({
      ...BRAIN, state: 'offline',
      state_detail: 'the agent service rejected our token',
    }))).toContain('rejected our token');
  });

  it('still says something useful with no detail', () => {
    expect(rowHint(row({ ...BRAIN, state: 'offline' }))).toBeTruthy();
  });

  it('leaves a running sandbox on its unknown-residency hint', () => {
    expect(rowHint(BRAIN)).not.toContain('not running');
  });

  it('never tells the owner a connected app\'s engine is "ready to answer"', () => {
    // Same sentence, same reason as the foreign case: it answers that app's
    // requests, not Ava's. Latent until the detail card stopped defaulting to
    // the brain — before that, an app row's hint was never rendered.
    expect(rowHint(APP_VLLM)).toBe('');
  });

  it('keeps the readings that ARE true of an app’s engine', () => {
    // "not Ava's" is not a reason to withhold a real diagnosis.
    expect(rowHint(row({ ...APP_VLLM, state: 'offline' })))
      .toBe('Nothing is answering at its address.');
  });
});

// --- memory: the denominator the panel never had ---------------------------

const pool = (memTotalGb: number | null, gpuTotalMb: number | null): MemPool => poolOf({
  gpu: { name: null, util: null, temp: null, power: null, mem_used_mb: null, mem_total_mb: gpuTotalMb },
  mem: { total_gb: memTotalGb, used_gb: null, free_gb: null, used_pct: null },
});

describe('poolOf', () => {
  it('calls a box with no GPU reading one system pool', () => {
    // The invisible-GPU case: a WSL2 box where nvidia-smi is unreachable. Every
    // row's figure is RAM there, so the sum is honest.
    expect(pool(15.4, null)).toEqual({ totalGb: 15.4, unified: true, noun: "this machine's" });
  });

  it('calls a Spark one pool despite the two totals never matching', () => {
    // 124620 MiB is 121.7 GB of the same silicon, but it arrives from a
    // different probe in a different unit minus whatever firmware reserves.
    expect(SPARK.unified).toBe(true);
    expect(SPARK.totalGb).toBe(121.7);
  });

  it('refuses to unify a discrete GPU', () => {
    // 48 GB of VRAM beside 128 GB of RAM: an nvidia-smi row and a docker row
    // are counted in different pools and must never be added.
    const p = pool(128, 49152);
    expect(p.unified).toBe(false);
    expect(p.totalGb).toBe(48);
    expect(p.noun).toBe("the GPU's");
  });

  it('invents no total when the machine reports none', () => {
    expect(pool(null, null)).toEqual({ totalGb: null, unified: false, noun: '' });
  });
});

describe('heldGb', () => {
  it('reads what a resident row is holding', () => {
    expect(heldGb(COMFY)).toBe(65.54);
  });

  it('is null, never zero, for a row that holds nothing', () => {
    // null and 0 are different sentences. A zero-width bar would silently agree
    // that "runs elsewhere" and "holds 0 GB" are the same reading.
    for (const state of ['remote', 'idle', 'absent', 'offline', 'unknown'] as const) {
      expect(heldGb(row({ state, memory_gb: 42 }))).toBeNull();
    }
    expect(heldGb(row({ state: 'resident', memory_gb: null }))).toBeNull();
  });
});

describe('shareOf', () => {
  it('divides by the pool, not by the biggest row', () => {
    // A 0.4 GB row on a 121.7 GB box is supposed to look like nothing.
    expect(shareOf(COMFY, SPARK)).toBeCloseTo(53.9, 1);
    expect(shareOf(row({ state: 'resident', memory_gb: 0.4 }), SPARK)).toBeCloseTo(0.33, 2);
  });

  it('draws nothing on a two-pool box', () => {
    expect(shareOf(COMFY, pool(128, 49152))).toBeNull();
  });

  it('draws nothing for a row that holds nothing, and nothing with no total', () => {
    expect(shareOf(ENV_BACKEND, SPARK)).toBeNull();
    expect(shareOf(COMFY, pool(null, null))).toBeNull();
  });
});

describe('memPhrase', () => {
  it('names the denominator the number is a share of', () => {
    expect(memPhrase(COMFY, SPARK)).toBe("54% of this machine's 121.7 GB");
  });

  it('still names it per row on a two-pool box, for the rows it fits', () => {
    // The SUM across rows mixes pools; an individual GPU reading does not.
    const discrete = pool(128, 49152);
    expect(memPhrase(row({ ...COMFY, memory_gb: 12 }), discrete))
      .toBe("25% of the GPU's 48.0 GB");
    expect(memPhrase(row({ ...COMFY, source: 'docker' }), discrete)).toBe('');
  });

  it('admits it has nothing to say rather than print an impossible share', () => {
    // 65.5 GB against a 48 GB pool means the denominator is wrong for this row.
    // Clamping would render the same broken reading as a confident "100%".
    expect(memPhrase(COMFY, pool(128, 49152))).toBe('');
  });

  it('says nothing when there is nothing to say', () => {
    expect(memPhrase(ENV_BACKEND, SPARK)).toBe('');
    expect(memPhrase(COMFY, pool(null, null))).toBe('');
  });
});

describe('groupMemoryGb', () => {
  it('adds up a section that shares one pool', () => {
    expect(groupMemoryGb([COMFY, OTHER_VLLM], SPARK)).toBeCloseTo(77.18, 2);
  });

  it('is null, never 0, when no row in the section reported one', () => {
    // A "0.0 GB" heading claims a measurement nobody made.
    expect(groupMemoryGb([BRAIN, ENV_BACKEND], SPARK)).toBeNull();
  });

  it('refuses to add VRAM to container RSS', () => {
    expect(groupMemoryGb([COMFY, OTHER_VLLM], pool(128, 49152))).toBeNull();
  });
});

describe('groupRows totals', () => {
  it('carries each section’s total when a pool is given', () => {
    const groups = groupRows([BRAIN, COMFY, OTHER_VLLM, ENV_BACKEND], SPARK);
    expect(groups.map((g) => g.relation)).toEqual(['brain', 'configured', 'foreign']);
    expect(groups[2].memoryGb).toBeCloseTo(77.18, 2);
    expect(groups[0].memoryGb).toBeNull();
  });

  it('keeps Ava’s own sections first, however much memory the others hold', () => {
    // Ranking sections by weight would put a stranger's 65 GB process above the
    // app the owner connected. Whose it is is what the order answers.
    const groups = groupRows([COMFY, APP_VLLM], SPARK);
    expect(groups.map((g) => g.relation)).toEqual(['app', 'foreign']);
  });
});

// --- row and card wording ---------------------------------------------------

describe('activityTone', () => {
  it('follows the state, and only shades it by busyness when that is known', () => {
    // The encoded bug: keying off gpu_util first painted a grey "idle" dot
    // beside a green "In memory", and grey on every CPU-only box.
    expect(activityTone(row({ state: 'resident' }))).toBe('ok');
    expect(activityTone(row({ state: 'resident', gpu_util: 63 }))).toBe('ok');
    expect(activityTone(row({ state: 'resident', gpu_util: 4 }))).toBe('warn');
    expect(activityTone(row({ state: 'resident', gpu_util: 0 }))).toBe('ok');
  });

  it('never lets busyness override a state that is not resident', () => {
    expect(activityTone(row({ state: 'offline', gpu_util: 99 }))).toBe('err');
    expect(activityTone(row({ state: 'absent' }))).toBe('warn');
    expect(activityTone(BRAIN)).toBe('muted');
  });
});

describe('rowSub', () => {
  it('gives an unnamed row its evidence rather than its state', () => {
    expect(rowSub(COMFY)).toBe('Holds flux2-vae, flux2_dev_fp8mixed +1');
  });

  it('keeps the no-evidence case short enough to not ellipse into nonsense', () => {
    // holdsLine's full sentence is right in the card and wrong in a ~183px row,
    // where it truncates to "Ava cannot tell what this progr…".
    expect(rowSub(row({ model_id: null, components: [] }))).toBe('Contents unknown');
  });

  it('spends no words on a model that is working', () => {
    // The dot is already green. "In memory" under a green dot is the same
    // reading twice, on the one row that needs no attention.
    expect(rowSub(OTHER_VLLM)).toBe('');
  });

  it('still states every reading that IS a diagnosis', () => {
    // The silence above is only earned by "everything is fine". A row that
    // needs acting on must say so without anyone clicking it.
    expect(rowSub(row({ backend: 'b', state: 'absent' }))).toBe('Not downloaded');
    expect(rowSub(row({ backend: 'b', state: 'offline' }))).toBe('Engine offline');
    expect(rowSub(row({ backend: 'b', state: 'idle' }))).toBe('Ready, not loaded');
    expect(rowSub(BRAIN)).toBe('Not observable');
  });
});

describe('listHint', () => {
  it('drops the hint for a model that is working', () => {
    // "Loaded and ready to answer." sat under "In memory" under a green dot.
    expect(listHint(row({ backend: 'b', state: 'resident' }))).toBe('');
  });

  it('keeps the diagnosis, and what the engine does have instead', () => {
    expect(listHint(row({ backend: 'b', state: 'absent', served: ['a', 'b'] })))
      .toBe('The engine is running but does not have this model yet. It has: a, b.');
  });
});

describe('MODEL_RELATION copy', () => {
  it('keeps every section heading short enough to be a fold control', () => {
    // The heading is the line the owner clicks, and at the narrowest the panel
    // gets the label track is ~105px — about eighteen characters at --fs-xs.
    // "Not Ava's — other software on this machine." was the foreign label for a
    // while and wrapped to three lines there.
    for (const r of RELATION_ORDER) {
      expect(MODEL_RELATION[r].group).toBeTruthy();
      expect(MODEL_RELATION[r].group.length).toBeLessThanOrEqual(20);
    }
  });

  it('puts the explanation in the note, where the fold can hide it', () => {
    // A section folded away costs its explanation nothing, so the note can
    // afford the whole thing — including the sentence that was cut for space
    // back when it rendered unconditionally.
    expect(MODEL_RELATION.foreign.note)
      .toContain('Not Ava’s — other software on this machine.');
    expect(MODEL_RELATION.foreign.note)
      .toContain('The memory it holds is not available to Ava.');
    expect(MODEL_RELATION.app.note).toBeTruthy();
  });

  it('gives Ava’s own sections nothing to explain', () => {
    // They are named by what they are. A note under "Ava's brain" would be
    // telling the owner what a brain is.
    expect(MODEL_RELATION.brain.note).toBe('');
    expect(MODEL_RELATION.configured.note).toBe('');
  });
});

describe('isAvas', () => {
  it('counts a registered backend as Ava’s, however unreachable', () => {
    // The bug: the panel's top-level cut was "is it the brain", under a heading
    // reading "Model use outside Ava". A backend the owner had registered
    // themselves was therefore filed as foreign — and then refused to fold,
    // because the fold rule knew it was Ava's even though the heading did not.
    expect(isAvas('brain')).toBe(true);
    expect(isAvas('configured')).toBe(true);
  });

  it('counts an app’s engine and a stranger’s as not Ava’s', () => {
    expect(isAvas('app')).toBe(false);
    expect(isAvas('foreign')).toBe(false);
  });

  it('splits every relation, so nothing lands in neither section', () => {
    expect(RELATION_ORDER.filter(isAvas).length
      + RELATION_ORDER.filter((r) => !isAvas(r)).length).toBe(RELATION_ORDER.length);
  });
});

describe('folding "Model use outside Ava"', () => {
  it('folds exactly the rows the heading is about', () => {
    // The panel has ONE fold, on the section heading, and `isAvas` is what
    // decides who is inside it — so the heading, the membership and the control
    // cannot drift apart. When the fold lived per-relation and the cut was "is
    // it the brain", a backend the owner had registered sat under a heading
    // calling it foreign and then refused to fold.
    const inside = [BRAIN, ENV_BACKEND, APP_VLLM, COMFY]
      .filter((m) => !isAvas(relationOf(m)));
    expect(inside).toEqual([APP_VLLM, COMFY]);
  });

  it('never puts Ava’s own subject inside the fold', () => {
    // A hardware panel that can hide Ava's brain, or the engines Ava routes to,
    // is not answering the question it exists for.
    expect(RELATION_ORDER.filter(isAvas)).toEqual(['brain', 'configured']);
  });

  it('keeps the memory total on the line that survives folding', () => {
    // The guarantee that makes closed-by-default honest: collapsing hides the
    // DETAIL, never the fact. The section's total is `groupMemoryGb` over every
    // row inside the fold, and the heading renders it open or shut.
    expect(groupMemoryGb([APP_VLLM, COMFY, OTHER_VLLM], SPARK))
      .toBeCloseTo(80.38, 2);
  });
});

describe('needsGroupHeads', () => {
  it('drops the heading when a section has one group', () => {
    // What the owner saw: "MODEL USE OUTSIDE AVA · 12.0 GB" and, one line
    // below, "Other software · 12.0 GB" — the same claim and the same number
    // twice, with the fold spent on the second copy.
    expect(needsGroupHeads(groupRows([COMFY, OTHER_VLLM], SPARK))).toBe(false);
  });

  it('keeps them when there is a distinction to draw', () => {
    // An app the owner connected is not a stranger's software, and that is the
    // one thing a group heading here is for.
    const groups = groupRows([APP_VLLM, COMFY], SPARK);
    expect(groups.map((g) => g.relation)).toEqual(['app', 'foreign']);
    expect(needsGroupHeads(groups)).toBe(true);
  });
});

describe('servedLine', () => {
  it('says what the engine DOES have when the model was never pulled', () => {
    expect(servedLine(row({ state: 'absent', served: ['a', 'b'] }))).toBe('It has: a, b.');
    expect(servedLine(row({ state: 'absent', served: ['a', 'b', 'c', 'd', 'e'] })))
      .toBe('It has: a, b, c +2 more.');
  });

  it('stays quiet when the model is not the problem', () => {
    expect(servedLine(row({ state: 'resident', served: ['a'] }))).toBe('');
    expect(servedLine(row({ state: 'absent', served: [] }))).toBe('');
  });
});

describe('componentMeta', () => {
  it('drops the word "configured" from a list where being listed IS configured', () => {
    expect(componentMeta({ kind: 'weights', kind_label: 'Saved model', in_memory: false }))
      .toBe('Saved model · not in memory');
    expect(componentMeta({ kind: 'weights', kind_label: 'Saved model', in_memory: true }))
      .toBe('Saved model · in memory');
    expect(componentMeta({ kind: 'vae', in_memory: null })).toBe('vae · residency unknown');
  });
});

describe('tempTone', () => {
  it('names a token instead of a hex that exists in neither theme', () => {
    expect(tempTone(90)).toBe('err');
    expect(tempTone(85)).toBe('err');
    expect(tempTone(70)).toBe('warn');
    expect(tempTone(52)).toBe('ok');
    expect(tempTone(null)).toBeNull();
  });
});
