window.__AVA_FIXTURES__ = {"alerts":{"ok":true,"active":[],"metrics":{"gpu_temp":61,"gpu_util":63,"mem_used_pct":66,"disk_used_pct":45,"error_rate":0.08}},"apps":{"apps":[{"id":"images","label":"Images","icon":"image","section":"apps","order":1,"embed":"iframe","url":"/apps/images/","has_api":false},{"id":"calendar","label":"Calendar","icon":"calendar","section":"apps","order":2,"embed":"iframe","url":"/apps/calendar/","has_api":false},{"id":"notes","label":"Notes","icon":"file","section":"apps","order":3,"embed":"iframe","url":"/apps/notes/","has_api":false}]},"brand":{"name":"Ava","tagline":"Your private AI — self-hosted, on your hardware."},"budget":{"currency":"USD","electricity_rate_per_kwh":0.16,"nominal_gpu_watts":300,"daily_spend_usd":0,"daily_energy_kwh":0.94,"power_measured":true,"budgets":{"daily_usd":2,"monthly_usd":40,"daily_kwh":3}},"chat-detail":{"id":"c_launch","title":"Launch poster + announcement","messages":[{"role":"user","content":"I'm launching Ava this week. Draft a short announcement and make a poster to go with it."},{"role":"assistant","content":"Love it. Here's a tight announcement, then I'll generate a poster to match.\n\n**Ava is live.** Your private AI — self-hosted on your own hardware, wired to your voice, your apps, and your creative tools. No accounts, no data leaving your machine. Just an assistant that actually answers to you.\n\nI'll make a poster in your teal-and-amber palette now.","model":{"id":"omni","label":"open-model 30B"},"tools_used":["images.generate"]},{"role":"user","content":"Perfect. Also block 30 minutes tomorrow morning to record the demo video."},{"role":"assistant","content":"Booked — **9:30–10:00 AM tomorrow: “Record Ava demo video.”** I added a reminder 15 minutes before, and dropped the announcement draft into your Notes so it's ready to paste.","model":{"id":"omni","label":"open-model 30B"},"tools_used":["calendar.create","notes.write"]}]},"chats":{"chats":[{"id":"c_launch","title":"Launch poster + announcement","updated":1751889200},{"id":"c_travel","title":"Weekend trip planning","updated":1751885600},{"id":"c_home","title":"Evening scene automation","updated":1751881000},{"id":"c_code","title":"Refactor the perf rollup","updated":1751878000}]},"connectors":{"ok":true,"action_count":37,"connectors":[{"id":"images","label":"Images (the GPU service)","kind":"creative","has_service":true,"has_perf":true,"egress_routes":2,"actions":["generate","upscale","variations"]},{"id":"calendar","label":"Calendar","kind":"productivity","has_service":true,"has_perf":false,"egress_routes":1,"actions":["read","create","update"]},{"id":"email","label":"Email","kind":"productivity","has_service":true,"has_perf":false,"egress_routes":1,"actions":["read","draft","send"]},{"id":"home","label":"Home Assistant","kind":"iot","has_service":true,"has_perf":false,"egress_routes":1,"actions":["control","state","scenes"]},{"id":"search","label":"Web Search (SearXNG)","kind":"knowledge","has_service":true,"has_perf":true,"egress_routes":1,"actions":["search","fetch"]},{"id":"notes","label":"Notes","kind":"productivity","has_service":true,"has_perf":false,"egress_routes":1,"actions":["read","write","search"]}]},"hardware":{"gpu":{"name":"NVIDIA RTX 6000 Ada","util":63,"temp":61,"mem_used_mb":38220,"mem_total_mb":49140},"mem":{"total_gb":128,"used_gb":84.6,"free_gb":43.4,"used_pct":66.1},"disk":{"total_gb":2048,"used_gb":913,"free_gb":1135,"used_pct":44.6},"cpu":{"util":21},"models":[{"id":"omni","name":"open-model 30B","model":"nemotron-open-model-30b","memory_mb":31860,"memory_gb":31.1,"gpu_util":63,"gpu_active":true,"pid":4821,"status":"serving","source":"vllm","role":"brain","in_memory":true}],"jobs":[],"ts":1751889600},"hub-agent-status":{"name":"Claude Agent","available":true,"runtime":"claude-agent-sdk","required":true,"cli":"claude","sandbox":"bubblewrap","sandbox_exists":true,"health":{"ok":true},"tools":true},"hub-approvals":{"pending":[]},"hub-audit":{"events":[{"ts":1751889560,"kind":"chat","status":"done","tools":["images.generate","notes.write"],"model":"open-model-30b","duration_s":7.2,"outcome":"ok","chat_id":"c_launch","request":"Draft a launch announcement and make a poster"},{"ts":1751889200,"kind":"code","status":"done","outcome":"committed","project":"ava","commit":"4c3a995","actor":"agent","approved_by":"you","paths":["ava_bridge/perf_store.py"],"request":"Refactor the perf rollup"},{"ts":1751888700,"kind":"connector","status":"done","connector":"calendar","tool":"calendar.update","outcome":"ok","request":"Move my 9am to 10:30"},{"ts":1751888100,"kind":"chat","status":"done","tools":["home.control"],"model":"open-model-30b","duration_s":2.1,"outcome":"ok","request":"Set the evening scene"},{"ts":1751887500,"kind":"learning","status":"done","outcome":"2 proposals parked","duration_s":41}]},"hub-backends":{"vllm":true,"ollama":true,"router":true},"hub-connectors":{"connectors":[{"id":"images","label":"Images (the GPU service)","kind":"creative","status":"up","actions":3,"mcp":false,"has_policy":true,"has_tools":true,"renders_policy":true,"enabled":true},{"id":"calendar","label":"Calendar","kind":"productivity","status":"up","actions":3,"mcp":true,"has_policy":true,"has_tools":true,"renders_policy":true,"enabled":true},{"id":"email","label":"Email","kind":"productivity","status":"up","actions":3,"mcp":true,"has_policy":true,"has_tools":true,"renders_policy":true,"enabled":true},{"id":"home","label":"Home Assistant","kind":"iot","status":"up","actions":3,"mcp":false,"has_policy":true,"has_tools":true,"renders_policy":true,"enabled":true},{"id":"search","label":"Web Search (SearXNG)","kind":"knowledge","status":"up","actions":2,"mcp":false,"has_policy":true,"has_tools":true,"renders_policy":true,"enabled":true},{"id":"notes","label":"Notes","kind":"productivity","status":"unknown","actions":3,"mcp":true,"has_policy":true,"has_tools":true,"renders_policy":true,"enabled":false}]},"hub-hardware":{"fit_gb":48,"source":"nvidia-smi","tier":"workstation","hint":"Comfortably fits a 30B model in VRAM with room for GPU workloads.","gpu":"NVIDIA RTX 6000 Ada"},"hub-models":{"detected_tier":"workstation","available_gb":48,"store":"~/.ava/models","roles":[{"role":"brain","id":"nemotron-open-model-30b","engine":"vllm","tier":"workstation","present":true},{"role":"vision","id":"open-model-vision","engine":"vllm","tier":"workstation","present":true},{"role":"embed","id":"bge-large-en","engine":"ollama","tier":"any","present":true},{"role":"speech","id":"whisper-large-v3","engine":"local","tier":"any","present":true}]},"hub-system":{"brand":"Ava","version":"1.0.0","code_approval":"all","learning_enabled":true,"learning_interval_h":24,"voice":true,"voiceprint":true,"web_search":true,"image":true,"docker":true,"retention_days":30,"retention_choices":[0,7,30,90]},"hub-voice":{"enabled":true,"deps_ok":true,"deps_error":"","enrolled":true,"threshold":0.62},"jobs":{"ok":true,"jobs":[{"id":"job_9f3a","kind":"image","status":"running","progress":62,"stage":"sampling","source":"images","created":1751889540,"updated":1751889598,"prompt":"cinematic product shot of a matte-black mini PC on a walnut desk, soft rim light"},{"id":"job_8c1d","kind":"image","status":"done","progress":100,"stage":"done","source":"images","created":1751889100,"updated":1751889141,"prompt":"isometric illustration of a home server rack, teal and amber palette"},{"id":"job_7b22","kind":"video","status":"done","progress":100,"stage":"done","source":"images","created":1751888300,"updated":1751888462,"prompt":"slow dolly across a neon-lit workstation, 3 second loop"},{"id":"job_6a04","kind":"upscale","status":"done","progress":100,"stage":"done","source":"images","created":1751887900,"updated":1751887904,"prompt":"upscale portrait to 4k"},{"id":"job_5f88","kind":"image","status":"error","progress":0,"stage":"error","source":"images","created":1751887000,"updated":1751887008,"prompt":"…","error":"model file missing"}]},"model":{"ok":true,"mode":"local","id":"omni","label":"open-model 30B","model":"nemotron-open-model-30b","engine":"vllm","base_url":"http://127.0.0.1:8000/v1","ctx_max":131072,"options":[{"id":"omni","label":"open-model 30B"}]},"ops-summary":{"ok":true,"jobs":{"running":1,"total":2841,"by_status":{"done":2830,"running":1,"error":10}},"turns":{"running":1,"total":14210,"by_status":{"done":14198,"running":1,"error":11}},"generations_24h":143,"learning":{"code":{"last_cycle":"2026-07-07T06:00:00Z","cycles":42,"pending":2},"chat":{"last_cycle":"2026-07-07T06:00:00Z","cycles":42,"pending":1}},"ts":1751889600},"perf-cost":{"ok":true,"since":"7d","spend_usd":0,"energy_kwh":6.83,"energy_usd":1.09,"power_measured":true,"avg_gpu_watts":214,"by":{"chat":{"spend_usd":0,"energy_kwh":3.41,"n":14210},"images":{"spend_usd":0,"energy_kwh":2.55,"n":2841},"video":{"spend_usd":0,"energy_kwh":0.71,"n":214},"voice":{"spend_usd":0,"energy_kwh":0.16,"n":1157}}},"perf-summary":{"ok":true,"apps":["ava","images","voice"],"total":18422,"sources_present":{"llm":true,"image":true,"video":true,"upscale":true},"summary":{"records":18422,"llm":{"open-model-30b":{"count":14210,"tokens_per_sec":{"n":14210,"avg":58.4,"p50":61,"min":22,"max":74},"ttft_ms":{"n":14210,"avg":412,"p50":388,"min":210,"max":980},"completion_tokens":{"n":14210,"avg":512,"p50":430,"min":12,"max":3900},"failovers":3}},"image":{"count":2841,"render_seconds":{"n":2841,"avg":6.2,"p50":5.8,"min":3.1,"max":14},"steps_per_sec":{"n":2841,"avg":8.9,"p50":9.1,"min":4,"max":12}},"video":{"count":214,"render_seconds":{"n":214,"avg":88,"p50":82,"min":41,"max":180},"steps_per_sec":{"n":214,"avg":2.3,"p50":2.2,"min":1.1,"max":3.4}},"upscale":{"count":1157,"seconds":{"n":1157,"avg":3.4,"p50":3.2,"min":1.8,"max":7}}}},"schedule":{"ok":true,"timers":[{"unit":"ava-learning.timer","description":"Nightly self-study cycle","activates":"ava-learning.service","next_time":"2026-07-08T06:00:00Z","next_rel":"in 7h","last_time":"2026-07-07T06:00:00Z","last_rel":"8h ago"},{"unit":"ava-perf-rollup.timer","description":"Hourly performance rollup","activates":"ava-perf-rollup.service","next_time":"2026-07-07T23:00:00Z","next_rel":"in 42m","last_time":"2026-07-07T22:00:00Z","last_rel":"18m ago"},{"unit":"ava-backup.timer","description":"Daily config + state backup","activates":"ava-backup.service","next_time":"2026-07-08T03:00:00Z","next_rel":"in 4h","last_time":"2026-07-07T03:00:00Z","last_rel":"19h ago"}]},"services":{"ok":true,"down":0,"services":[{"name":"Bridge (web + API)","unit":"ava-bridge","systemd":"ava-bridge.service","probe_ok":true,"status":"up"},{"name":"Inference (vLLM)","unit":"vllm","systemd":"vllm.service","probe_ok":true,"status":"up"},{"name":"Agent runtime","unit":"ava-agent","systemd":"ava-agent.service","probe_ok":true,"status":"up"},{"name":"Image engine (the GPU service)","unit":"gpusvc","systemd":"gpusvc.service","probe_ok":true,"status":"up"},{"name":"Voice gateway","unit":"ava-voice","systemd":"ava-voice.service","probe_ok":true,"status":"up"},{"name":"Search (SearXNG)","unit":"searxng","systemd":"searxng.service","probe_ok":true,"status":"up"}]},"setup-connectors":{"connectors":[{"id":"images","label":"Images (the GPU service)","kind":"creative"},{"id":"calendar","label":"Calendar","kind":"productivity"},{"id":"email","label":"Email","kind":"productivity"},{"id":"home","label":"Home Assistant","kind":"iot"},{"id":"search","label":"Web Search (SearXNG)","kind":"knowledge"},{"id":"notes","label":"Notes","kind":"productivity"}]},"tools":{"ok":true,"total_calls":5842,"tools":[{"tool":"web.search","count":1310},{"tool":"images.generate","count":984},{"tool":"calendar.read","count":742},{"tool":"shell.run","count":611},{"tool":"notes.write","count":508},{"tool":"weather.forecast","count":431},{"tool":"code.edit","count":389},{"tool":"email.read","count":356},{"tool":"home.control","count":274},{"tool":"upscale.image","count":237}]},"turns":{"ok":true,"turns":[{"id":"turn_a91","status":"running","created":1751889580,"step_count":4,"last_step":{"kind":"tool","name":"web.search","text":"searching: RTX 6000 Ada idle power draw"},"tools_used":["web.search"],"model":{"label":"open-model 30B","id":"omni"},"ctx_tokens":5120,"has_job":false,"error":null,"reply_preview":"Pulling the latest figures now…"},{"id":"turn_a90","status":"done","created":1751889200,"step_count":7,"last_step":{"kind":"text","name":null,"text":"Done — poster saved to your Images."},"tools_used":["images.generate","notes.write"],"model":{"label":"open-model 30B","id":"omni"},"ctx_tokens":8410,"has_job":true,"error":null,"reply_preview":"I generated three poster options and saved your favorite."},{"id":"turn_a89","status":"done","created":1751888700,"step_count":3,"last_step":{"kind":"text","name":null,"text":"Your 9am is moved to 10:30."},"tools_used":["calendar.read","calendar.update"],"model":{"label":"open-model 30B","id":"omni"},"ctx_tokens":3200,"has_job":false,"error":null,"reply_preview":"Rescheduled and sent the update."},{"id":"turn_a88","status":"done","created":1751888100,"step_count":5,"last_step":{"kind":"text","name":null,"text":"Living-room lights set to 30%."},"tools_used":["home.control"],"model":{"label":"open-model 30B","id":"omni"},"ctx_tokens":1900,"has_job":false,"error":null,"reply_preview":"Done — evening scene is on."}]}};
// Browser-side API mock: makes the real Ava SPA run entirely client-side on
// sample data, with no backend. It patches window.fetch and EventSource so every
// /api/* call is answered from fixtures. Loaded BEFORE the app bundle so the
// patch is in place before the first request.
//
// Fixtures are injected ahead of this file as `window.__AVA_FIXTURES__` (see the
// demo build). This is the browser twin of marketing/src/engine/mock.ts.
(function () {
  var FIX = window.__AVA_FIXTURES__ || {};
  var ANCHOR = 1751889600; // seconds; fixtures are anchored here (2025-07-07)

  function offsetSec() { return Math.floor(Date.now() / 1000) - ANCHOR; }

  // Shift epoch-second timestamps onto "now" so relative labels read fresh.
  function rebase(v) {
    if (v === undefined || v === null) return {};
    var off = offsetSec();
    var KEYS = { created: 1, updated: 1, ts: 1, since_ts: 1 };
    function walk(x) {
      if (Array.isArray(x)) return x.map(walk);
      if (x && typeof x === 'object') {
        for (var k in x) {
          if (KEYS[k] && typeof x[k] === 'number' && x[k] > 1e9) x[k] = x[k] + off;
          else x[k] = walk(x[k]);
        }
        return x;
      }
      return x;
    }
    return walk(JSON.parse(JSON.stringify(v)));
  }

  var wob = function (i, k) { k = k || 1; return 0.5 + 0.5 * Math.sin(i * 0.7 * k + k); };

  function series(metric) {
    var N = 24, step = 3600000, end = Date.now(), pts = [];
    for (var i = 0; i < N; i++) pts.push({ t: end - (N - 1 - i) * step, 'tokens/s': Math.round(42 + 30 * wob(i) + 8 * wob(i, 3)) });
    return { ok: true, metric: metric, bucket: '1h', series: ['tokens/s'], points: pts };
  }
  function hwHistory() {
    var N = 48, step = 300000, end = Date.now(), s = [];
    for (var i = 0; i < N; i++) s.push({
      ts: end - (N - 1 - i) * step,
      gpu_util: Math.round(35 + 55 * wob(i)), gpu_temp: Math.round(48 + 22 * wob(i, 2)),
      gpu_power: Math.round(180 + 140 * wob(i)), mem_used_pct: Math.round(58 + 20 * wob(i, 1.5)),
      mem_used_gb: +(74 + 24 * wob(i, 1.5)).toFixed(1), cpu: Math.round(12 + 30 * wob(i, 2.5)),
    });
    return { ok: true, samples: s };
  }
  function fx(name) { return rebase(FIX[name]); }

  // A canned assistant reply so the chat composer feels alive in the demo.
  var DEMO_REPLY =
    'This is a live demo running on sample data, right in your browser. In a real ' +
    'install, Ava would answer here using your own local model and tools, and nothing ' +
    'would leave your machine. Explore the tabs on the left to see the Command Center, ' +
    'Operations, and browser Setup.';

  var ROUTES = [
    [/^\/api\/health$/, function () { return { ok: true, model: 'Nemotron open-model 30B', ctx_max: 131072, ctx_base: 8192 }; }],
    [/^\/api\/brand$/, function () { return fx('brand'); }],
    [/^\/api\/apps$/, function () { return { apps: [] }; }], // no left-rail iframe apps in the static demo
    [/^\/api\/model$/, function () { return fx('model'); }],
    [/^\/api\/hardware\/history$/, hwHistory],
    [/^\/api\/hardware$/, function () { return fx('hardware'); }],
    [/^\/api\/perf\/summary$/, function () { return fx('perf-summary'); }],
    [/^\/api\/perf\/series$/, function (p, u) { return series(u.searchParams.get('metric') || 'tokens_per_sec'); }],
    [/^\/api\/perf\/recent$/, function () { return { ok: true, recent: [] }; }],
    [/^\/api\/perf\/cost$/, function () { return fx('perf-cost'); }],
    [/^\/api\/hub\/cost$/, function () { return fx('budget'); }],
    [/^\/api\/setup\/hardware$/, function () { return fx('hub-hardware'); }],
    [/^\/api\/setup\/backends$/, function () { return fx('hub-backends'); }],
    [/^\/api\/setup\/connectors$/, function () { return fx('setup-connectors'); }],
    [/^\/api\/hub\/system$/, function () { return fx('hub-system'); }],
    [/^\/api\/hub\/agent\/status$/, function () { return fx('hub-agent-status'); }],
    [/^\/api\/hub\/connectors$/, function () { return fx('hub-connectors'); }],
    [/^\/api\/hub\/models\/pull\/status$/, function () { return { status: 'idle', role: null, rc: null, log: [] }; }],
    [/^\/api\/hub\/models\/bench\/status$/, function () { return { status: 'idle', result: null }; }],
    [/^\/api\/hub\/models$/, function () { return fx('hub-models'); }],
    [/^\/api\/hub\/voice\/status$/, function () { return fx('hub-voice'); }],
    [/^\/api\/hub\/audit$/, function () { return fx('hub-audit'); }],
    [/^\/api\/hub\/approvals$/, function () { return fx('hub-approvals'); }],
    [/^\/api\/ops\/summary$/, function () { return fx('ops-summary'); }],
    [/^\/api\/ops\/services$/, function () { return fx('services'); }],
    [/^\/api\/ops\/schedule$/, function () { return fx('schedule'); }],
    [/^\/api\/ops\/tools$/, function () { return fx('tools'); }],
    [/^\/api\/ops\/alerts$/, function () { return fx('alerts'); }],
    [/^\/api\/ops\/connectors$/, function () { return fx('connectors'); }],
    [/^\/api\/jobs$/, function () { return fx('jobs'); }],
    [/^\/api\/turns$/, function () { return fx('turns'); }],
    // Chat: return the demo conversation, and answer new messages with a canned reply.
    [/^\/api\/chat-stream$/, function () { return { turn_id: 'demo-turn' }; }],
    [/^\/api\/turn\/[^/]+$/, function () { return { id: 'demo-turn', status: 'done', steps: [], tools_used: [], reply: DEMO_REPLY, job: null }; }],
    [/^\/api\/chats\/[^/]+$/, function () { return fx('chat-detail'); }],
    [/^\/api\/chats$/, function (p, u, method) { return method === 'POST' ? { id: 'demo-new' } : fx('chats'); }],
  ];

  function resolve(pathname, url, method) {
    for (var i = 0; i < ROUTES.length; i++) {
      if (ROUTES[i][0].test(pathname)) {
        try { return ROUTES[i][1](pathname, url, method); }
        catch (e) { return { ok: false, error: String(e) }; }
      }
    }
    return { ok: true };
  }

  var IS_API = /\/(api|apps|internal|login|logout)(\/|$)/;

  var _fetch = window.fetch ? window.fetch.bind(window) : null;
  window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    var method = (init && init.method) || (input && input.method) || 'GET';
    try {
      var u = new URL(url, location.href);
      if (IS_API.test(u.pathname)) {
        var body = resolve(u.pathname, u, method.toUpperCase());
        return Promise.resolve(new Response(JSON.stringify(body), {
          status: 200, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
        }));
      }
    } catch (e) { /* fall through to the real fetch */ }
    return _fetch ? _fetch(input, init) : Promise.reject(new Error('offline'));
  };

  // SSE stub: the streaming Ops feed opens an EventSource. Keep it "open" with no
  // events so the polled fixtures render and there's no reconnect error spam.
  var RealES = window.EventSource;
  window.EventSource = function (url) {
    var isApi = false;
    try { isApi = IS_API.test(new URL(url, location.href).pathname); } catch (e) {}
    if (!isApi && RealES) return new RealES(url);
    var es = {
      url: url, readyState: 1, withCredentials: true,
      close: function () { this.readyState = 2; },
      addEventListener: function () {}, removeEventListener: function () {},
      onopen: null, onmessage: null, onerror: null,
    };
    setTimeout(function () { if (typeof es.onopen === 'function') es.onopen({}); }, 0);
    return es;
  };

  // A small "sample data" ribbon so demo viewers know it's not their instance.
  window.addEventListener('DOMContentLoaded', function () {
    var r = document.createElement('div');
    r.textContent = 'Live demo — sample data';
    r.style.cssText = 'position:fixed;z-index:99999;bottom:10px;left:10px;padding:4px 10px;' +
      'font:600 11px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;color:#fff;letter-spacing:.02em;' +
      'background:rgba(224,103,63,.92);border-radius:999px;pointer-events:none;box-shadow:0 4px 14px rgba(0,0,0,.35)';
    document.body.appendChild(r);
  });
})();
