# Browser automation

ARIA can control a persistent Chromium-family browser through Playwright. Browser automation is an optional ARIA capability and is intentionally separate from the desktop launcher system.

## Enable it

Install the Playwright runtime:

```bash
uv run playwright install chromium
```

Then configure:

```yaml
browser:
  enabled: true
  headless: false
  profile_directory: data/browser-profile
  executable_path: ""
  timeout_ms: 15000
  max_text_chars: 30000
  wait_until: domcontentloaded
  viewport_width: 1440
  viewport_height: 900
  launch_args: []
```

## Persistent profile

The configured profile directory stores browser state such as cookies and login sessions. This is what allows an ARIA browser session to remain authenticated across restarts.

**Never reuse the profile directory of a browser that is already running as your normal daily browser.** A dedicated profile prevents ARIA automation from interfering with the user's ordinary browser session.

Treat `data/browser-profile/` as sensitive local data.

## Browser vs desktop browser

These are two different mechanisms:

```text
browser tools
    └── Playwright-controlled browser session

 desktop.launchers.browser
    └── native application launcher, e.g. Firefox
```

Setting `browser.enabled: true` does not automatically launch the native desktop browser. Likewise, configuring a desktop `browser` launcher does not create Playwright automation.

## Runtime controls

The browser service is designed around explicit actions such as navigation, tab handling, page snapshots/text extraction, clicking, typing, keyboard input, screenshots, downloads, and uploads.

Page extraction is bounded by `max_text_chars` so a large document cannot consume the entire model context.

Action timeouts are controlled by `timeout_ms`. `wait_until` controls the page-load condition Playwright waits for.

## Headless vs visible

```yaml
headless: false
```

is useful while developing/debugging because the operator can see the browser.

Headless mode is useful for background automation but makes it harder to visually diagnose unexpected navigation or authentication problems.

## Security considerations

Browser automation can interact with real authenticated accounts. A model-controlled browser can therefore:

- read private web content;
- submit forms;
- upload files;
- download files;
- follow links;
- potentially trigger external actions.

Do not treat browser automation as equivalent to a read-only scraper.

Use a dedicated account/profile where practical, minimize stored credentials, and do not give the profile access to accounts you would not trust an automation agent to operate.

## Launch arguments

`launch_args` allows Chromium arguments to be configured. Add arguments only when there is a concrete reason and verify that they do not weaken browser isolation or security.

## Troubleshooting

### Chromium executable missing

Run:

```bash
uv run playwright install chromium
```

### Login disappears after restart

Check `profile_directory`. A persistent profile must point to the same directory each time.

### Browser conflicts with normal Chromium/Chrome

Use a dedicated ARIA profile. Never point ARIA at the active profile of your everyday browser.

### Page extraction is incomplete

Check `max_text_chars` and whether the target page requires JavaScript-rendered content. Browser automation can interact with rendered pages, but extracted text remains intentionally bounded.

### Automation times out

Check `timeout_ms` and `wait_until`. Avoid simply raising timeouts indefinitely; a page that never reaches the selected condition may make every action slow.
