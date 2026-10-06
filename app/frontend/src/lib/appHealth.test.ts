import { describe, expect, it } from 'vitest';
import { healthReasons, healthTitle } from './appHealth';
import type { AppHealth } from './types';

// The copy layer. The backend decides the VERDICT (dashboard._app_verdict);
// everything here is about never showing the owner a colour they cannot act on
// — and never blaming an app for a setting it does not have.

const app = (over: Partial<AppHealth> = {}): AppHealth => ({
  id: 'device-app',
  health: 'ready',
  enabled: true,
  service: 'up',
  auth_env: 'DEVICE_APP_KEY',
  auth_set: true,
  tools_expected: 2,
  tools_deployed: true,
  policy_expected: true,
  policy_present: true,
  ...over,
});

describe('healthReasons', () => {
  it('is empty when nothing is missing', () => {
    expect(healthReasons(app())).toEqual([]);
  });

  it('never claims a missing credential for an app with no credential slot', () => {
    // auth_env null means the manifest declares no auth. Saying "no credential
    // saved" would send the owner hunting for a field that does not exist.
    expect(healthReasons(app({ auth_env: null, auth_set: false }))).toEqual([]);
  });

  it('names the env var when a credential IS missing', () => {
    expect(healthReasons(app({ auth_set: false }))[0]).toContain('DEVICE_APP_KEY');
  });

  it('says nothing about tools an app does not ship', () => {
    expect(healthReasons(app({ tools_expected: 0, tools_deployed: false }))).toEqual([]);
  });

  it('says nothing about a policy the app does not render', () => {
    expect(healthReasons(app({ policy_expected: false, policy_present: false }))).toEqual([]);
  });

  it('reports every missing piece at once', () => {
    expect(healthReasons(app({
      service: 'down', auth_set: false, tools_deployed: false, policy_present: false,
    }))).toHaveLength(4);
  });

  it('distinguishes a probe that said no from one it could not read', () => {
    expect(healthReasons(app({ service: 'down' }))[0]).toContain('not answering');
    expect(healthReasons(app({ service: 'unknown' }))[0]).toContain('could not be read');
  });
});

describe('healthTitle', () => {
  it('names the app, so a tooltip is never an orphan verdict', () => {
    expect(healthTitle('Health App', app())).toContain('Health App');
  });

  it('does not imply Ava watched an app answer when it declares no probe', () => {
    const t = healthTitle('Studio', app({ service: null }));
    expect(t).toContain('declares no health check');
    expect(t).not.toContain('Answering');
  });

  it('claims a live answer only when there was one', () => {
    expect(healthTitle('Health App', app())).toContain('Answering');
  });

  it('explains a partial verdict instead of just naming it', () => {
    const t = healthTitle('Device App', app({ health: 'partial', auth_set: false }));
    expect(t).toContain('partly ready');
    expect(t).toContain('DEVICE_APP_KEY');
  });

  it('treats off as a choice, never a fault', () => {
    const t = healthTitle('Device App', app({ health: 'off', enabled: false, service: 'down' }));
    expect(t).toContain('switched off');
    expect(t).not.toContain('not answering');
  });

  it('says it is still checking before the first reading lands', () => {
    expect(healthTitle('Device App', undefined)).toContain('checking');
  });
});
