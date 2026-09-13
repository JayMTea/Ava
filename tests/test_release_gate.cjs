const assert = require('node:assert/strict');
const {test} = require('node:test');
const gate = require('../deploy/scripts/require_ci.js');

const good = {id: 1, head_sha: 'selected', event: 'push', status: 'completed',
  conclusion: 'success', head_repository: {full_name: 'owner/product'}, html_url: 'verified-run'};

async function check(runs) {
  const errors = [], info = [];
  await gate({sha: 'selected', context: {repo: {owner: 'owner', repo: 'product'}},
    github: {rest: {actions: {listWorkflowRuns: {}}}, paginate: async (_, args) => {
      assert.equal(args.workflow_id, 'ci.yml');
      assert.equal(args.head_sha, 'selected');
      return runs;
    }}, core: {setFailed: error => errors.push(error), info: text => info.push(text)}});
  return {errors, info};
}

test('publishing accepts successful push CI for precisely the selected commit', async () => {
  const result = await check([good]);
  assert.equal(result.errors.length, 0);
  assert.match(result.info[0], /verified-run/);
});

test('publishing refuses absent, foreign, wrong-commit, failed or pending CI', async () => {
  for (const runs of [[], [{...good, head_sha: 'other'}], [{...good, event: 'pull_request'}],
    [{...good, head_repository: {full_name: 'stranger/fork'}}],
    [good, {...good, id: 2, conclusion: 'failure'}],
    [good, {...good, id: 2, status: 'in_progress', conclusion: null}]]) {
    assert.equal((await check(runs)).errors.length, 1);
  }
});
