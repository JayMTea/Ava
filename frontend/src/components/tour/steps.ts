// What the first-run walkthrough says, per page.
//
// Copy rules, taken from the surfaces this sits on top of: plain sentences, "it"
// rather than "she", no emoji and no hype. One or two sentences per step — this
// is orientation, not documentation.
//
// The rule that matters most: this file does NOT explain what a metric means.
// frontend/src/components/dashboard/metrics.ts is the one glossary, reused by
// every ⓘ across Vitals, Operations and Learning, and docs/capabilities/vitals.md
// makes that single-source-ness an explicit promise. So the Vitals step teaches
// the ⓘ affordance instead of restating it — the user leaves knowing where every
// future answer lives, and there is no second copy to drift.
// tests/test_tour_steps.py fails if a step body duplicates a glossary string.

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

  vitals: [
    {
      target: '.db-kpis',
      title: 'How hard this machine is working',
      body: 'These update live while Ava runs. Hover the ⓘ beside any number to '
        + 'see exactly what it measures.',
    },
    {
      target: '[data-tour="vitals-budget"]',
      title: 'Spend and energy',
      body: 'Measured against caps you set in Setup. Caps only raise alerts — '
        + 'nothing is ever blocked mid-answer.',
    },
    {
      target: '[data-tour="vitals-apps"]',
      title: 'Per-app cost',
      body: 'Every app you connect shows up here on its own, with what it cost to '
        + 'run.',
    },
  ],

  ops: [
    {
      target: '[data-tour="ops-live"]',
      title: 'What is happening now',
      body: 'In-flight answers and renders, streaming as they run. Empty is the '
        + 'normal state.',
    },
    {
      target: '[data-tour="ops-alerts"]',
      title: 'Things worth knowing',
      body: 'Budget, hardware and service problems Ava has flagged.',
    },
    {
      target: '[data-tour="ops-health"]',
      title: 'Is each piece answering',
      body: 'The first place to look when something feels broken — it shows which '
        + 'service stopped responding.',
    },
  ],

  data: [
    {
      // The page's own tab bar, not a filter strip inside a panel. Spotlight
      // takes the FIRST match, so a bare `.hub-tabs` used to work by DOM-order
      // luck; the child combinator says which one is meant.
      target: '.hub-inner > .hub-tabs',
      title: 'Everything Ava has kept',
      body: 'What it remembers, your conversations, and the logs behind these '
        + 'dashboards.',
    },
    {
      title: 'It is yours to delete',
      body: 'Any of it can be removed from here. It lives on this machine and '
        + 'nowhere else.',
    },
  ],
};

/** Page ids with a walkthrough, in the order a first-run user meets them. */
export const TOUR_PAGES = Object.keys(TOURS);
