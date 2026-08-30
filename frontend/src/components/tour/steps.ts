// What the first-run walkthrough says, per page.
//
// Copy rules, taken from the surfaces this sits on top of: plain sentences, "it"
// rather than "she", no emoji and no hype. One or two sentences per step — this
// is orientation, not documentation.
//
// This file orients; it does not document. Keep step bodies to what the user
// needs in order to act on the page they are looking at.

export interface TourStep {
  /** CSS selector for the element to spotlight. Missing is normal, not an error
   *  — panels legitimately render an empty state — and falls back to a centered
   *  card carrying the same copy. */
  target?: string;
  title: string;
  body: string;
}

/** Keyed by the App view id. Keys must exist in BUILTIN_VIEWS and in the
 *  server-side PAGES allowlist (ava_bridge/hub/tour.py); a guard test asserts
 *  both, because a mismatch means a walkthrough that runs and can never be
 *  recorded as seen — so it would run forever. */
export const TOURS: Record<string, TourStep[]> = {
  agent: [
    {
      target: '.agent-list',
      title: 'The agent console',
      body: 'You talk to the agent in Chats. This console is where you watch '
        + 'it work: every session it has open, grouped — your own chats '
        + 'included, under their own heading.',
    },
    {
      target: '.agent-bar',
      title: 'Three views of the same agent',
      body: 'Sessions is what is happening now, Activity is what already '
        + 'happened, and Automations is what runs without you. Configuration '
        + 'lives in Setup, not here.',
    },
    {
      target: '.agent-chip',
      title: 'Whether it is actually connected',
      body: 'This says whether Ava can reach the agent right now. Open it for '
        + 'the settings behind it.',
    },
  ],
  hub: [
    {
      target: '.ov-cards',
      title: 'This is Setup',
      body: 'Everything is configured from here. There is no terminal step and no '
        + 'config file to edit: your changes are written to ava.yaml for you.',
    },
    {
      target: '[data-tour="hub-hardware"]',
      title: 'Your hardware',
      body: 'Ava measured this machine during install and sized itself to fit. '
        + 'Nothing to do here unless the hardware changes.',
    },
    {
      target: '[data-tour="hub-agent"]',
      title: 'The model is the brain',
      body: 'A model is the file that does the actual thinking, and you confirmed '
        + 'one during install. Change it here, along with tools and memory.',
    },
    {
      target: '[data-tour="hub-connectors"]',
      title: 'Your own apps',
      body: 'Point Ava at an app you already run and it becomes a tab Ava can '
        + 'watch and drive.',
    },
  ],

  chat: [
    {
      target: '#composer',
      title: 'Ask for things here',
      body: 'Plain language is enough. This is the part you will use most.',
    },
    {
      target: '[data-tour="model-chip"]',
      title: 'What is thinking',
      body: 'The model answering right now. Click it to switch to another one you '
        + 'have available.',
    },
  ],

};

/** Page ids with a walkthrough, in the order a first-run user meets them. */
export const TOUR_PAGES = Object.keys(TOURS);
