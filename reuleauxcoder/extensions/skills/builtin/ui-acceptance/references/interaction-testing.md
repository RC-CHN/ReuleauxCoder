# Practical interaction testing

## Choose an executable path

Start with the application's existing test scripts, package dependencies and launch instructions. Check which browser, desktop driver, image viewer and terminal facilities are actually available. A skill supplies instructions, not a browser, desktop session or image-reading capability.

- **Web page or webview:** use an available browser driver, or the project's Playwright installation with its installed browser. Load the current application build. A separately authored HTML concept validates only that concept.
- **Native editor or desktop app:** launch an isolated instance with a temporary workspace/profile through the project's host test runner or available desktop automation.
- **TUI:** launch the real program under a PTY, or ConPTY on Windows. Piped stdin/stdout tests do not exercise terminal interaction.

Reuse existing dependencies. If setup is needed, install the selected automation package and its browser in a task-local environment according to the project's package manager. Record missing dependencies or launch restrictions; fix the specific cause instead of disabling browser security by default. Keep the app server and driver in a compatible network namespace. Wait for a ready response or visible control, and close the processes and browser contexts you create.

Identify the actual execution hosts. In a remote editor, the browser/clipboard can belong to the local machine while the extension, core and files belong to the workspace host. A label saying “SSH” in a fixture does not establish a remote connection.

## Drive the browser and save evidence

Prefer role/name or label locators discovered from the current page. Use stable test IDs when necessary. Avoid coordinate guesses and positional selectors for ordinary controls. Coordinate input is useful when testing hit targets or an interface without semantic elements; inspect the current screenshot before using it.

The following Node example uses `playwright` and Node's standard library. Save it as an `.mjs` file where the project's Playwright dependency resolves, start the application separately, then run `node journey.mjs <application-url>`. Adapt the accessible names and expected behavior to the real interface. This example expects a textbox named `Message` and a log named `Conversation`; it does not create either control or provide a backend.

The default launch uses Playwright's matching browser installation. If the environment provides a compatible Chromium elsewhere, set `PLAYWRIGHT_CHROMIUM_EXECUTABLE` to that executable and record the browser used.

```javascript
import assert from 'node:assert/strict';
import {mkdtemp} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {chromium} from 'playwright';

const url = process.argv[2];
if (!url) throw new Error('Usage: node journey.mjs <application-url>');
const output = await mkdtemp(join(tmpdir(), 'ui-acceptance-'));
console.log(`Evidence directory: ${output}`);
const browser = await chromium.launch({
  headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
});
try {
  const page = await browser.newPage({viewport: {width: 360, height: 800}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(url);
  const input = page.getByRole('textbox', {name: 'Message', exact: true});
  const transcript = page.getByRole('log', {name: 'Conversation', exact: true});
  await input.waitFor({state: 'visible'});
  await transcript.waitFor({state: 'visible'});
  await page.evaluate(async () => {await document.fonts.ready;});
  await page.screenshot({path: join(output, '01-before.png')});

  const text = `Acceptance message ${Date.now()}`;
  await input.fill(text);
  await input.press('Enter');
  assert.equal(await input.inputValue(), '', 'Clear the submitted draft');
  assert.equal(await transcript.getByText(text, {exact: true}).count(), 1,
    'Show the submitted message once');
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    'Keep the page within the narrow viewport');
  await page.screenshot({path: join(output, '02-submitted.png')});
  assert.deepEqual(errors, []);
} finally {
  await browser.close();
}
```

This checks the visible submission path at the time of observation. It does not prove backend acceptance, persistence or a latency bound. Check the application's acknowledgement and resulting data separately. For regressions that must be immediate, use the controlled slow-response journey below; a fast backend can hide a frontend that waits for acknowledgement.

For a failing journey, retain a screenshot of the failing state and the console/request errors before teardown. If supported, start tracing or video before the first action and save it on failure. Name evidence by the actual state, locale and viewport. A successful screenshot call establishes only that an image was written.

## Exercise timing and recovery through the UI

Prepare fixture data before starting the journey. For a webview fixture, document which host messages are simulated. During verification, use visible controls; do not set application state, alter CSS/DOM to hide defects or call the handler being tested directly. Read-only DOM checks may corroborate what is visible.

For chat submission, including steering during an active response:

1. Arrange a test backend or transport fixture that holds the submission acknowledgement until the test explicitly releases it. Enter the running state through the normal flow where possible; label a simulated running state as fixture coverage.
2. Type a message and press Enter. While acknowledgement is still held, check that the input clears and exactly one user message appears with an honest pending status. Use an explicit responsiveness budget when timing is part of the requirement; a long retrying assertion can conceal delayed feedback.
3. Enter a new draft, then release the acknowledgement. Confirm that the new draft survives, the original message updates in place and a second empty Enter did not submit another message.
4. Repeat with a controlled failure, then use the visible retry action. Confirm understandable feedback and no duplicated message. Check cancellation or navigation away where relevant.

Wait for observable changes such as an enabled button, dismissed panel or updated delivery status. Avoid arbitrary sleeps for functional assertions. Streaming apps may never reach network-idle. Timed sampling is appropriate for studying animation or measuring delay, but state what was measured.

Beyond the happy path, choose cases relevant to the change: empty/long input, IME composition, Shift+Enter, keyboard-only navigation, Escape/back, focus recovery, loading, reconnect, failure/retry and a repeated click. Check whether a user can discover the next action from the screen without knowing internal commands.

## Inspect screenshots and motion

Open the saved PNG through the session's actual image-reading facility. A shell file listing, OCR result, DOM assertion or successful screenshot call is not a visual review. If image viewing is unavailable, report that limitation and retain the images for review instead of claiming appearance passed.

Inspect the task entry point and the important before/after states at the user's viewport. Check readable text, information hierarchy, contrast, clipping, overlays, scroll position, focus indication and whether the next action is obvious. Test the relevant languages and themes, including long translated labels. Use both a viewport screenshot and, when needed, a full-page image; the latter alone can hide the need to scroll or an awkward sticky footer.

For animations, keep normal motion enabled first. Capture successive frames or a video spanning the transition and inspect them; extracting and viewing frames is sufficient when video playback is unavailable. Verify that feedback starts promptly, text stays readable and approval targets do not move under the pointer. CSS animation names only establish that an animation is configured, not that it looks correct. Repeat with reduced motion and verify that controls and feedback still work. Disabling animation for a stable baseline screenshot does not validate the animation itself.

## Verify the owning host

| Path exercised | What it establishes | What still needs separate coverage |
| --- | --- | --- |
| Browser + current frontend + simulated host messages | Rendering and interactions for the supplied states | Real host dispatch, persistence and native surfaces |
| Synthetic clipboard event or file-input assignment | The frontend receives and processes those bytes | OS clipboard formats/permissions, paste shortcut, native picker and remote transfer |
| Real editor + commands/API calls | Extension activation, native documents/diffs, host command behavior and file effects | Button discoverability, pointer/focus behavior and the click-to-command connection |
| Desktop driver clicks + real core/workspace | The connected journey in that tested environment | Other operating systems or remote configurations not exercised |
| PTY/ConPTY + real TUI input | Terminal key handling, redraw, resize and lifecycle | Appearance in other terminal/font environments |

For editor integration, use the real host to inspect the native diff, original/modified documents and file content before and after approval. Include an unsaved buffer and a stale review when relevant. If invoking a host command directly, label the result as host integration coverage. To establish the full user path, activate the visible approval control through available desktop automation and verify the resulting file change. Inspect permission-policy controls separately from a pending edit approval: changing whether a tool may run and accepting one proposed edit are different journeys.

For clipboard/uploads, paste a known file through the actual OS clipboard when that facility is available, then verify its name and bytes at the intended destination host. A browser-created `ClipboardEvent` is useful isolated coverage, but cannot establish OS paste. Likewise, selecting a file through an automation API does not prove the native file chooser works. In Remote, verify which machine owns the source, uploaded file and editor document; a local integration run alone does not cover this boundary.

For a TUI, send real terminal key sequences, resize during output, navigate menus and verify focus/cancellation. Parse captured terminal output with a compatible terminal emulator for screen-state assertions. Inspect a rendered terminal capture for visual claims; raw escape sequences are not a viewed screenshot. Close the PTY and child processes after the test.

## Report evidence at its actual scope

Record the journey, tested build, host/backend setup, OS, viewport, theme/locale, expected and observed result, and evidence location. Separate passed, failed and untested paths. State which screenshots or recordings were actually viewed and which services or events were simulated. Keep generated evidence in the agreed artifact location; do not add it to source control unless requested or required by the project.

For example: “The browser journey passed with a held acknowledgement; before/after PNGs were viewed. Native diff approval passed through editor commands. The OS clipboard shortcut and Remote SSH journey were not exercised.” This is more useful than an unqualified claim that the UI passed. After a fix, rerun the failed journey and the affected regressions.
