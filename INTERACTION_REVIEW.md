# Interaction review

Status: reviewed again after the interaction changes. All findings below are addressed; no open blockers were found in the reviewed scope. This review covers interaction behavior without requesting palette or typography changes.

## Findings

| Original priority | Finding | Resolution and evidence |
| --- | --- | --- |
| High | The initial drawer could not move below its existing 43% lower bound. | Added three snap positions, including a 64px minimized state. Browser touch verification reported a 95px downward drag moving the 301px drawer to 206px under the finger, then settling at 64px. Regression tests pass. |
| Medium | After scrolling to an estimate, only a small, unlabeled handle remained available for collapse. | A persistent 44px handle now labels its action, supports tap/keyboard controls, and restores the previous open state. Minimized content is inert. Native result scrolling is retained. |
| Medium | Mobile searches placed the selected marker behind the expanded drawer; region fitting assumed a fixed drawer height. | Marker positioning uses the target drawer height, and region fitting measures its current height. Browser verification reported the marker at y=133–161 above the drawer's y=235 upper edge. |
| Medium | The initial state's 520px maximum height conflicted with larger drag heights. | Shared drawer geometry now controls all snap states and overlays; conflicting mobile minimum/maximum limits were removed. Code review and snap-position tests pass. |
| Medium | Reduced motion did not cover JavaScript map inertia and keyboard panning. A follow-up review also found runtime pinch/fade paths. | The map overrides ordinary pan/view calls, disables inertia and pinch-release animation when reduced motion is active, and disables cached JavaScript tile fading. Runtime preference changes update inertia and pinch options. Overrides were checked against vendored Leaflet 1.9.4. Browser verification reported zero-second CSS transitions. |
| Medium | Cancelled searches retained a permanent “Finding that address…” status. | Cancellation clears status, aborts the request, and ignores late responses. The new cancellation regression test passes. |
| Medium | A delayed estimate could override manual content scrolling. | Pointer, wheel, and keyboard interactions suppress delayed automatic scrolling. Tests confirm native content events are not prevented. Browser verification reported content scroll changing from 181px to 883px while the drawer stayed 525px high. |
| Medium | A delayed geocode response could reopen a drawer minimized while lookup was pending. | Manual minimization cancels address lookup. The new integration-style helper test confirms a late response cannot select a location or reopen the 64px drawer. |
| Low | A rejected horizontal drag could produce a click and accidentally collapse the drawer. | Movement beyond tap tolerance suppresses the following click even when no vertical drag begins. The new horizontal-movement, release, and click regression test passes. |

## Existing strengths

- Search suggestions support arrow keys and Enter, Escape cancellation, escaped labels, and protection against stale responses.
- Estimate requests abort obsolete work and hide stale marker prices immediately.
- Native dialogs have accessible names, close controls, Escape behavior, and backdrop dismissal.
- Map resizing observes real element dimensions, and the shading remains geographically attached during movement.

## Verification and limitations

The independent reviewer reran all 28 tests across `test_frontend_interactions.js` (18), `test_map_resize.js` (7), and `test_surface_motion.js` (3); all passed. Final code review confirmed the fixes for delayed geocoding, runtime motion preferences, and horizontal gesture clicks. These tests exercise extracted helpers and do not themselves simulate browser layout or native gesture arbitration.

The browser evidence in the table was supplied by the root agent using actual browser touch-event dispatch. The root agent also verified that Escape closes the dialog and returns focus. Physical iOS/Android touch behavior, mobile keyboard variations, and screen-reader navigation remain unverified. A map loaded with reduced motion keeps zoom animation disabled if the preference is later turned off; this avoids changing Leaflet's initialization-dependent animation capability. No private data, provider configuration, payment settings, or external actions were inspected or changed for this review.
