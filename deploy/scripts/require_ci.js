// Used by actions/github-script before either a deployment or a public release.
module.exports = async ({github, context, core, sha}) => {
  const {owner, repo} = context.repo;
  const runs = await github.paginate(github.rest.actions.listWorkflowRuns, {
    owner, repo, workflow_id: 'ci.yml', head_sha: sha, per_page: 100,
  });
  const trusted = runs.filter(run => run.head_sha === sha && run.event === 'push'
    && run.head_repository?.full_name === `${owner}/${repo}`);
  trusted.sort((a, b) => b.id - a.id);
  if (!trusted.length || trusted[0].status !== 'completed' || trusted[0].conclusion !== 'success') {
    core.setFailed(`The latest push CI run for ${sha} must complete successfully before publishing or deploying.`);
    return;
  }
  core.info(`Verified CI run ${trusted[0].html_url} for commit ${sha}`);
};
