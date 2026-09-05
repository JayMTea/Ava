# Ava on your phone (PWA)

**Optional, and it comes after the four setup steps.** Do
[step 1: Install](../deploy/README.md) through
[step 4: Connect your apps](CONNECT_YOUR_APPS.md) on a computer first; this
page adds your phone to an Ava that already works.

**At the end of this page:** Ava on your home screen, full-screen with its own
icon, reachable from wherever you are. **How long:** about two minutes on the
phone, plus one command on the Ava machine if you have not set up remote access
yet.

Ava's web app is an installable **Progressive Web App** (a website a phone can
add to its home screen and run like an installed app). The same responsive UI
serves desktop and mobile - there is no separate app to install or keep
updated. New frontend builds are picked up automatically the next time the app
is opened.

## Install

1. Open your Ava URL in the phone's browser and log in.
2. **iOS (Safari):** Share → *Add to Home Screen*.
   **Android (Chrome):** ⋮ menu → *Install app* (some builds still say
   *Add to Home screen*). Chrome may also offer to install it on its own
   once you've used Ava a few times.

| iPhone | Android |
|---|---|
| ![iPhone: in Safari, tap Share, then Add to Home Screen](assets/pwa-install-ios.png) | ![Android: in Chrome, tap the three-dot menu, then Install app](assets/pwa-install-android.png) |

## HTTPS is required for the full experience

Two things need a *secure context* (`https://` or `http://localhost`): the
service worker that precaches the app shell, and - on Android - Chrome's
offer to install the app at all. Over plain HTTP on your LAN
(`http://192.168.x.x:8096`) the app still works and can still be added to
the home screen (on iOS 26 and later it even opens standalone, with its own
icon), but there is no offline shell and no Android install prompt.

Reaching Ava on a LAN address at all is opt-in: the bridge binds `127.0.0.1`
by default, and Docker publishes on `127.0.0.1` too, so
`http://192.168.x.x:8096` is refused until you widen `server.host` in
`ava.yaml` - or the published port in `deploy/docker-compose.yml`. Prefer a
VPN or a TLS-terminating proxy over binding `0.0.0.0` on a network you don't
control; `tailscale serve` needs no change.

The easiest way to get HTTPS for a self-hosted Ava is
[Tailscale](https://tailscale.com). One-time: enable HTTPS certificates for
your tailnet (admin console → DNS → HTTPS Certificates) - the interactive
CLI offers to do this for you, but `--bg` will not prompt. Then, on the Ava
machine:

```sh
tailscale serve --bg 8096
```

That publishes the bridge inside your tailnet at
`https://<host>.<tailnet>.ts.net` with a valid certificate - no port
forwarding, no certificate management, and nothing exposed to the public
internet. Open that URL on any device in your tailnet and install from
there. Any other reverse proxy that terminates TLS (Caddy, nginx +
Let's Encrypt) works the same way.

### Logging in over plain HTTP

Ava decides the session cookie's `Secure` flag per request (`auth.cookie_secure:
auto`, the default), so signing in at `http://192.168.x.x:8096` works - and
behind a TLS-terminating proxy the cookie is still marked `Secure`, because the
bridge reads `X-Forwarded-Proto` from peers listed in `server.trusted_proxies`
(loopback by default, which covers `tailscale serve` and a same-host Caddy).

Pin it to `true` if you always front Ava with TLS, or to `false` only on a
network you trust:

```yaml
auth:
  cookie_secure: auto   # true | false | auto
```

If you set `true` and then browse over plain HTTP, the browser drops the cookie
and every login bounces back to the sign-in screen with no error - that looks
exactly like a wrong password. `auto` exists to avoid that.

## Staying signed in

You should type the password once per device, not once per launch.

**The session slides forward on use.** Every authenticated request re-issues the
cookie once it is past halfway through its life, so `auth.session_ttl_days`
(default 30) is an **inactivity** window - a phone you open daily never reaches
it. Before this, the expiry was stamped at sign-in and never moved, so a
daily-driver phone was still signed out on day 31.

```yaml
auth:
  session_ttl_days: 30   # days WITHOUT USE before you sign in again
```

**Let the phone hold the password.** The sign-in form is a standard password
form (`autocomplete="current-password"` with a hidden account field), so iOS
offers to save it to iCloud Keychain the first time and fills it with Face ID
after that. If you dismissed that offer once, iOS stops asking: add it by hand
in **Settings -> Passwords -> +**, using your Ava URL as the site.

**One origin, or one cookie per origin.** A cookie belongs to the exact host you
signed in at, so `http://192.168.1.50:8096` at home and
`https://spark.tail1234.ts.net` away are two separate sessions, and moving
between them looks exactly like being logged out. Install the home-screen app
from the URL that works from *everywhere* - with `tailscale serve` that is the
tailnet name, on the LAN as well as off it.

## What is (and isn't) cached offline

The service worker precaches only the **app shell** - the HTML, JS, CSS and
icons. Everything live (chat turns, `/api/*`, media, uploads, connector
apps) is network-only and never served stale. Offline, the app opens but
Ava herself is unreachable, exactly as you'd expect.

## Scope

This is the governed-cockpit surface on mobile: chat, dashboards, the Data
page (browse/export everything Ava stores), and the Setup hub. Voice capture
uses the browser microphone (also secure-context-gated). There are no push
notifications yet.

---

**Next:** [What Ava does](capabilities/index.md) - every capability, one page
each. For what reaching Ava from outside the house does and does not open up,
see [Security model](../SECURITY.md).
