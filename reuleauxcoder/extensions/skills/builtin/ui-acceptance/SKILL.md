---
name: ui-acceptance
description: Verify interfaces through real user journeys, visible feedback, keyboard interaction and visual evidence.
---

# Test the user's actual path

Identify the entry point, desired outcome and visible success condition. Use supplied reproduction steps directly. For an open-ended request, explore the main path and prioritize it. Prepare the environment within existing authorization.

Use browser/editor automation actually available in the session. Disclose missing GUI, screenshot viewing or native-host coverage. A web mock does not validate VS Code's native editor; a TUI needs an actual terminal/PTY path.

## Act and observe

- Click, type, paste, scroll and use keys through visible controls. Fixture preparation may write data, but verification must not inject application state or call internal handlers to bypass the UI.
- After the main path, test empty/long input, duplicate submission, waiting, failure/retry, cancellation, navigation back and focus recovery.
- On message submission, check prompt clearing, immediate user-message display and preservation of a newer unsent draft.
- Check whether the next action and current state are discoverable. Cover narrow layouts, light/dark themes, keyboard use, long/localized strings and reduced motion where relevant.
- Actually view screenshots for hierarchy, contrast, clipping, overlap and animation; corroborate with read-only DOM or accessibility state. Saving a screenshot without viewing it is not visual validation.
- Wait for observable transitions. Animations must not move an approval target during interaction or delay essential feedback.

Record blocked paths with steps, expected/actual behavior, environment and evidence; continue independent paths. When fixes are authorized, rerun the failed path and related regressions after editing.

Report passed, failed and untested journeys with evidence. Native diffs, file paste and permission policy controls must be exercised in their owning host before claiming end-to-end success.
