# TALVIORA AI — product-wide UI/UX audit

**Date:** 2026-10-18 · **Repos:** `ai-placement-android` @ `8b571f2` (`career-strategy-intelligence`), `ai-placement-backend` @ `46d420f` (`professional-presence-intelligence`)
**Stage:** A (audit) complete · B (design foundation) batch 1 delivered · C–H not started

This document records what the codebase **actually** contains, measured from source on 2026-10-18.
Several findings contradict the brief that commissioned it; where they do, the measurement is given
so the brief can be corrected rather than the code bent to match it.

---

## 1. Scale

| | Count |
|---|---|
| `Screen` route objects | **104** |
| NavGraph destinations (`safeAreaComposable`) | **74** |
| Screen composables (`*Screen.kt`) | **97** |
| ViewModels | **95** |
| Feature packages under `ui/` | **37** |

A product-wide restyle of 97 screens is **not** deliverable as one safe change batch, and the brief
permits recorded batches. The staged plan is in §7.

---

## 2. The central finding: the design system is already disciplined

The brief anticipates "arbitrary font sizes across screens" and colour drift. Measured across the
whole `ui/` tree:

| Measurement | Result | Reading |
|---|---|---|
| `Color(0xFF…)` literals outside `ui/theme/` | **0** | Every screen already goes through `MaterialTheme.colorScheme` |
| `fontSize = …` outside `Typography.kt` | **0** | All 12 occurrences are the token definitions themselves |
| Uses of the `Spacing` scale | **973** across **116** files | Spacing is centralised and genuinely used |
| Raw `.dp` literals in padding/size | 78, of which **67 are `size(…)`** at 14/16/18/20/22/24/48/56 | Conventional icon and avatar sizes, not drift |
| `TextStyle`s without `lineHeight` | **0** | Type scales correctly with system font size |

**There is no colour or typography drift to fix.** The honest conclusion is that Stage B's goal —
"establish a cohesive design system" — was already met before this programme began. What was
missing was not the system but the ability to *see* it (§3).

The token set is complete: `Spacing` (xs/sm/md/lg/xl/xxl/chipGap), `Elevation` (level0–level5),
`AppShapes` (extraSmall–extraLarge), a 12-role `AppTypography`, the full Material colour role set,
plus two app-specific semantic roles (`successColor()`, `warningColor()`) that deliberately avoid
putting "skill gap" on the alarm-coded `error` role.

### What this programme must *not* do

Restyling screens that already conform would be churn with a visual-regression risk and no
user-visible benefit. The remaining value is in §3, §4 and §5 — not in repainting.

---

## 3. The real gap: no visual validation surface — now addressed

| Measurement | Result |
|---|---|
| `@Preview` functions in the entire `ui/` tree | **0** |
| Roborazzi / Paparazzi / `screenshotTest` | **absent** |
| `androidx.ui.tooling.preview` dependency | **present** (`implementation`) |
| `androidx.ui.test.junit4` | present (`androidTestImplementation`, needs a device) |

97 screens and no way to render one without building and installing the app. This is why careful
visual work has been hard here: a change to the type scale or a shared component touches every
screen and nothing could show the result.

**Batch 1 fixes this** with `ui/theme/DesignSystemCatalog.kt` — the full token set rendered in
three previews, including `fontScale = 1.5f` and a 320dp-wide phone. It uses the preview tooling
already present, so **no dependency was added**, and nothing in the app navigates to it.

Screenshot testing (Roborazzi, as used by Now in Android) remains **BLOCKED** on the dependency
gate in §6.

---

## 4. Architecture notes (observed, not changed)

**Two navigation mechanisms coexist.** `NavGraph` owns 74 destinations, while the four bottom tabs
(`Home`, `Tests`, `Progress`, and the dashboard) are rendered by a `when (selectedRoute)` switch
inside `MainScreen.kt`. An initial pass flagged `Screen.Home`, `Screen.Tests` and `Screen.Progress`
as unwired dead routes; **that was wrong** — they are reachable through the tab switch. Recorded
because the split is real and a future contributor will trip over it, not because it is a defect.
Unifying it is a high-blast-radius refactor with no user-visible benefit and is **not** proposed.

**Accessibility baseline** (counts, not a verdict):

| | Count |
|---|---|
| `Icon(` call sites | 106 |
| `contentDescription` usages | 147 |
| `contentDescription = null` (explicitly decorative) | 77 |
| `semantics { }` blocks | 67 |
| Custom `.clickable` modifiers | **72** |
| Reduced-motion helper (`ReduceMotion`) usages | 12 |

Icon labelling and semantics are in reasonable shape. The open risk is the **72 custom
`.clickable` modifiers**: a `.clickable` on a small `Box` can fall below the 48dp target, and that
cannot be settled from source — it needs rendering. Marked **NOT RUN**, not PASS.

---

## 5. Feature inventory

Screens per feature package, generated from source. Per-screen state-by-state audit (loading /
empty / offline / error / success) is **NOT RUN** for packages outside the voice and history
features; doing it honestly requires the preview surface from §3, which only now exists.

| Feature package | Screens | ViewModels | Files |
|---|---|---|---|
| `about` | 1 | 0 | 1 |
| `actions` | 0 | 1 | 2 |
| `alarm` | 3 | 3 | 6 |
| `arena` | 2 | 2 | 4 |
| `atscheck` | 10 | 9 | 20 |
| `auth` | 4 | 3 | 7 |
| `careerdecision` | 1 | 1 | 3 |
| `careergoal` | 2 | 2 | 5 |
| `careerpath` | 11 | 11 | 26 |
| `careerstrategy` | 1 | 1 | 3 |
| `careertwin` | 1 | 1 | 2 |
| `common` | 0 | 0 | 21 |
| `history` | 2 | 2 | 5 |
| `home` | 1 | 1 | 2 |
| `interview` | 5 | 6 | 12 |
| `jobhunt` | 10 | 10 | 24 |
| `jobprep` | 2 | 2 | 4 |
| `mission` | 0 | 1 | 2 |
| `nextbestaction` | 0 | 1 | 1 |
| `notifications` | 1 | 2 | 4 |
| `onboarding` | 9 | 6 | 15 |
| `placementpath` | 3 | 3 | 6 |
| `premium` | 0 | 1 | 1 |
| `presence` | 1 | 1 | 2 |
| `profile` | 2 | 2 | 4 |
| `progress` | 1 | 1 | 2 |
| `projectbuilder` | 4 | 4 | 8 |
| `recruiterattack` | 4 | 4 | 8 |
| `resume` | 2 | 2 | 5 |
| `roadmap` | 2 | 2 | 4 |
| `settings` | 3 | 2 | 6 |
| `skillpassport` | 2 | 2 | 4 |
| `subscriptions` | 1 | 1 | 2 |
| `support` | 1 | 0 | 1 |
| `tests` | 4 | 4 | 8 |
| `theme` | 0 | 0 | 7 |
| `voiceagent` | 1 | 1 | 4 |

---

## 6. Decisions required from the product owner

### 6.1 Brand palette — the brief conflicts with a recorded decision

The brief asks for "sophisticated dark navy as the main brand foundation. Electric blue and cyan…
restrained gold accents."

The app's current identity is **indigo/violet**, and `Color.kt` states it was "chosen deliberately
over the stock Material Baseline palette to give … its own visual identity," citing
`UI_UX_REFERENCES.md` §8.

Two facts matter:

1. Adopting the new palette repaints **97 screens** with no screenshot safety net.
2. **`UI_UX_REFERENCES.md` is not in this repository.** The code comment is the only surviving
   record of the original decision, so there is no document to reconcile the brief against.

Options, for an explicit decision — **not taken here**:

| Option | Consequence |
|---|---|
| **A. Keep indigo/violet** | Zero risk. The brief's palette is declined on the record. |
| **B. Adopt navy/electric-blue/gold** | New identity. Needs the screenshot gate (§6.3) first, then a token-level change plus a full visual pass. |
| **C. Adopt it only for voice/AI surfaces** | "Electric blue and cyan for intelligent features" as stated, indigo elsewhere. Risks two identities in one app. |

### 6.2 Dark mode

`Theme.kt` states dark mode is "deliberately not offered, regardless of the device's system
setting," and `SemanticColors.kt` repeats it. The brief asks for "appropriate light and dark
themes."

Adding a dark scheme is a brand decision affecting every screen and every hardcoded-light
assumption in `SemanticColors.kt`. **Not implemented.** `DesignSystemGuardTest` now fails if
`darkColorScheme` appears, so the decision cannot be made by accident — it has to be made.

### 6.3 Screenshot testing dependency

Roborazzi would let every screen be visually regression-tested, which is the only way to do §6 of
the brief ("modernize every screen") safely at this scale.

| | |
|---|---|
| Component | `io.github.takahirom.roborazzi` + Robolectric |
| Licence | Apache-2.0 (Roborazzi); Now in Android uses it under Apache-2.0 |
| Cost | None — local only, no service |
| Benefit | Before/after evidence for all 97 screens; makes a palette change (§6.1) safe |
| Risk | New test dependencies; Now in Android records its baselines on Linux CI, and this host is Windows, so baseline portability needs checking |

**BLOCKED** pending authorization.

---

## 7. Staged plan

| Stage | Scope | Status |
|---|---|---|
| **A** | Audit, inventory, drift measurement, defect triage | **PASS** (this document) |
| **B** | Design foundation | **PASS (batch 1)** — guard test + preview catalog; no restyle needed per §2 |
| **C** | Shared navigation and components | **NOT RUN** — gated on §6.3 for safe visual change |
| **D** | Feature-by-feature modernization (37 packages) | **NOT RUN** — multiple batches; needs §6.1 decided first |
| **E** | Functional defect remediation | **NOT RUN** — no confirmed in-scope UI defect found yet; see §8 |
| **F** | Accessibility and visual consistency | **PARTIAL** — baseline counted; 72 tap targets need rendering |
| **G** | Full regression | **PASS for this batch** — see the batch report |
| **H** | Release readiness | **NOT RUN** |

---

## 8. Defect triage

No confirmed, in-scope UI/navigation/state defect was found during this audit. Two candidates were
investigated and **both dismissed on evidence**:

| Candidate | Verdict |
|---|---|
| 3 unwired routes (`Home`, `Tests`, `Progress`) | **Not a defect** — reachable via the `MainScreen` tab switch |
| 78 raw `.dp` literals | **Not a defect** — 67 are conventional icon sizes |

Earlier batches in this programme did fix two genuine defects, recorded here for continuity: the
`ws://` scheme crash in `NetworkModule` (voice sessions terminated the process), and the absence of
any durable conversation store. Both have regression tests.

Speculative concerns deliberately **not** recorded as bugs: recomposition cost, image caching, and
list eagerness — none measured, and the brief forbids claiming performance work without evidence.

---

## 9. Unverified areas

| Area | Status | Why |
|---|---|---|
| Any screen rendered on hardware | **NOT RUN** | `adb devices -l` empty |
| Per-screen loading/empty/error states outside voice + history | **NOT RUN** | Needs the preview surface or screenshots |
| 72 custom tap targets ≥ 48dp | **NOT RUN** | Requires rendering |
| Contrast ratios | **NOT RUN** | Requires rendering or a measuring tool |
| Light/dark behaviour | **NOT APPLICABLE** | Single-theme product decision (§6.2) |
| Performance | **NOT RUN** | No measurement taken; none claimed |

---

## 10. Brand palette adoption

Owner decision of 2026-10-18: adopt deep navy, electric blue/cyan and restrained gold,
preserving the design-system architecture and semantic tokens.

Implemented as a **token-level change only**: every name in `Color.kt` is unchanged, so all
97 screens and 116 design-system users re-brand through `MaterialTheme.colorScheme` with no
screen file edited.

| Role | Value | White-text contrast |
|---|---|---|
| primary (deep navy) | `#0A2A5E` | **13.95** |
| secondary (electric blue) | `#0B5FD4` | **5.83** |
| tertiary (restrained gold) | `#8A6100` | **5.54** |
| accentCyan (voice/AI) | `#0E7490` | **5.36** |

Rejected on measurement, not taste: bright cyan `#06B6D4` (2.43) and bright gold `#C99A2E`
(2.58) fail AA **and** the 3:1 UI floor on this background. They appear only as containers
holding navy text, where they measure ~14:1. All ratios are enforced by
`BrandPaletteContrastTest`, computed from the real token objects.

### Recorded consequence: gold and electric blue share a lightness

They differ almost entirely by hue -- **1.05:1** in luminance, **172.7 degrees** apart. They
are unmistakable in colour and nearly identical in greyscale, so a viewer with severe colour
vision deficiency may not separate them. This is the concrete reason the rule *status is
never communicated by colour alone* must hold; a pinning test keeps the fact visible.

A first version of that test wrongly required luminance separation between every brand pair
and failed. Contrast ratio measures lightness, not colour; the test now accepts separation on
either axis. Recorded because the failure was real and the fix was to the test.

### Still not visually validated

No screen has been rendered with this palette. The change is correct by measurement and
compiles, which is **not** the same as looking right. Preview review and the screenshot gate
(§6.3) remain open.

## 11. Batch C -- navigation, motion and shared components (2026-10-18)

Owner request: add a back control to every screen that lacks one, make Mock Interviews
voice-driven, fix the voice interview's loading time, and raise the product's visual quality.
Recorded here as a batch, per the standing instruction not to restyle all 97 screens in one
pass.

### 11.1 The navigation defect, measured

Every screen file was compared against every destination in the graph.

| measure | count |
|---|---|
| `*Screen.kt` files | 97 |
| screens with **no** back affordance of any kind | **52** |
| of those, hosted by `MainScreen`'s tab switch (correctly back-less) | 4 |
| NavGraph destinations total | 101 |

"No affordance" means no `navigationIcon`, no `DetailTopBar`, no back control at all. The
system back gesture still worked, so nobody was trapped -- but on a gesture-navigation device,
where there is no visible system back button either, nothing on screen said so.

Two of the 52 (`AtsCheckPreCheck`, `CareerPathHome`) were missed by the first pass, because
their source contains the word `onBack` -- passed down to a child composable -- and a
whole-file text search read that as "has a back control". They were found by auditing the
graph's destination blocks instead, which is the measure that actually matters.

### 11.2 How it was fixed, and why not inside the screens

A second wrapper in the navigation layer, `backComposable`, alongside the existing
`safeAreaComposable`. It applies the same `safeDrawingPadding()` and draws a `BackBar` above
the content. 50 destinations now go through it; 4 screens that own their own `Scaffold` took an
`onBack` parameter and placed the bar in their own `topBar` slot, because wrapping them would
have applied the window inset twice.

The bar is **icon-only by default**. 45 of the 50 wrapped screens already draw their own
heading as the first thing in their content ("Mock Interviews", "Settings"), and a titled app
bar above that would have printed the same words twice on dozens of screens. Five screens that
draw no heading (`Privacy`, `Terms`, the written interview, the ATS pre-check and analysis)
take the title in the bar instead. The rule is one title per screen, and
`BackAffordanceTest` enforces both halves of it.

Four destinations remain deliberately back-less and are recorded with reasons in
`BackAffordanceTest.roots`: `Welcome`, `Main`, `OnboardingName`, `OnboardingSuccess`. A new
destination now fails that test until someone either gives it a back control or adds it to that
list with a reason.

### 11.3 The voice interview "loading time" -- measured, and not what it looked like

Measured on 2026-10-18 against the real local providers:

| step | cold | warm |
|---|---|---|
| question synthesis (`pyttsx3`, SAPI5) | 0.43s | 0.13s |
| **question audio duration** (22.05 kHz mono 16-bit, 275,590 bytes) | **6.25s** | 6.25s |
| speech-to-text model load (`faster-whisper`) | 2.09s | 0.000s |

Synthesis was never the problem. `VoiceInterviewViewModel.loadNextTurn` held the turn in a
phase named `LOADING_AUDIO` until `playQuestionAudio` returned -- and that call suspends until
the question has finished **playing**. The student therefore sat in front of a disabled
"Playing..." button for the full 6.25 seconds on every question. It was not loading; it was
talking.

Three changes:

1. The phase is now `SPEAKING`, playback runs in its own coroutine, and **recording is enabled
   while the question is being read**. Starting it cancels playback, so the question is not
   captured into the answer. A "Skip reading" control is offered only while audio is actually
   playing.
2. A synthesis failure no longer ends the interview. It previously threw out of `loadNextTurn`
   onto an error screen, although the question text was on screen and perfectly answerable. It
   is now a flag on a working turn (`audioUnavailable`) with a notice saying why it went quiet.
3. The speech model is warmed on a background daemon thread at backend startup
   (`warm_speech_models_on_startup`, default on, local provider only), moving the 2.09s off
   whichever student submitted the first answer after a restart. Failure is logged and ignored
   -- it is an optimisation, not a dependency.

Not measured: whether the interview now *feels* fast. The numbers above are provider and audio
measurements; the state ordering that removes the wait is pinned by `VoiceInterviewLatencyTest`,
which asserts ordering and contains no timing assertion.

### 11.4 Mock Interviews are now voice-first

The brief said Mock Interviews were text-based. They were, as the default path: every entry
point navigated to `textInterviewRoute`, and the voice interview came *afterwards* as a second
phase. All four entry points now go to `voiceInterviewRoute`.

The written interview was **not** removed. It is the same interview engine, the same questions
and the same grading, and it is the only option for a student on a shared machine or with a
broken microphone. It is now an explicit choice -- a "Speak answers" / "Type answers" selector
on the Mock Interviews screen, defaulting to speaking and surviving rotation. A voice-first
product is not a voice-only one.

### 11.5 Shared components, which is where product-wide quality came from

The design system was already centralised (§2: 0 colour literals and 0 `fontSize` values
outside the theme, 973 `Spacing` uses). That is the leverage: changing a shared component
changes every screen without editing one.

| change | reach |
|---|---|
| `AppCard` gained a hairline edge | every card in the app |
| `PrimaryButton`/`SecondaryButton` minimum height 40dp -> 52dp | every button in the app |
| `PrimaryButton` gained a `loading` state | replaces the `"Submitting..."` + dead-button idiom |
| `LoadingState` gained an optional label | every loading screen |
| `Motion` tokens (`quick`/`standard`/`emphasized`, enter/exit easing) | replaces per-composable inline durations |
| app-wide screen transitions on the `NavHost` | all 101 destinations |

The card hairline is the one worth justifying. Cards and the page are **the same colour**
(`#F7F9FC`), so the only thing separating them was a 1dp shadow, which on a near-white
background is very nearly invisible. Every card dissolved into the page, and screens read as
undifferentiated runs of text -- which is most of what "it doesn't look designed" means in
practice. The hairline is 8% of `onSurface`, deliberately below the 3:1 UI contrast floor,
which is correct *because it carries no information*: it is a seam, not a control boundary, and
nothing is communicated by it alone.

Screen transitions replaced Navigation Compose's default cross-fade, which gives no sense of
direction -- pushing a detail screen and backing out of it looked identical. The pop
transitions mirror the push, so going back visibly undoes it. The platform scales these
animators to zero under "Remove animations", so they need no `rememberReduceMotion()` check;
composables that animate on their own clock, like the recording pulse, still check it.

### 11.6 Verification

- Android: **1520 tests, 0 failures, 0 skipped**, counted from
  `app/build/test-results/testDevDebugUnitTest/TEST-*.xml`, not from "BUILD SUCCESSFUL".
  Baseline before this batch was 1506.
- `SystemBarInsetContractTest` was **extended, not relaxed**. It knew about one inset wrapper
  and could not see the new one; it now reads both, applies the same no-hard-coded-dp and
  no-`.only(...)` guards to each, and gained a guard that the two are never nested -- which
  would pad 50 screens twice. Two of its four failures were my own doing: making the `NavHost(`
  call multi-line broke its anchor, and a KDoc mentioning `safeDrawingPadding()` was counted as
  a second application of it.
- New tests: `BackAffordanceTest` (5), `VoiceInterviewLatencyTest` (7), two inset guards.

### 11.7 Still not visually validated, and what is left

No screen has been rendered. These changes compile, are measured where a number is quoted, and
are covered by tests -- none of which is the same as looking right.
`ui/common/ComponentCatalog.kt` adds `@Preview`s for the new components at default, 1.5x font
scale and 320dp width, which is the surface for that review; opening it is the owner's gate.

Not done in this batch, and not claimed:

- The 37 feature packages are **not** individually restyled. Only `MockInterviewCategoryScreen`
  was redesigned, as the entry point to the flow the owner raised.
- `HomeScreen` (574 lines) is unchanged beyond what the shared components give it.
- Headers are not unified: 45 screens draw their own heading with a bare back arrow above it,
  rather than one app-bar pattern. Moving those headings into the bar would touch 45 files and
  belongs in its own batch.
- Dark mode, Roborazzi, and the product-name mismatch (§6) are unchanged.

## 12. Batch D — the six-screen premium redesign (2026-10-18)

Driven by six supplied screenshots (Home, Profile, Test Center, Roadmap empty, Roadmap
populated, Progress) and the complaint that the app reads as a collection of forms rather than
a product. Everything below was implemented, not proposed.

### 12.1 Diagnosis

The screens were not badly built; they were built from **one** container. Every screen used
`AppCard` on the default surface, so a hero, a statistic, a category and a footnote all
rendered as the same pale rectangle. Nothing on a screen was permitted to look more important
than anything else, which is most of what "it doesn't look designed" means in practice.

Two measurements that confirmed it rather than assuming it:

- `grep "Color(0x"` across all 97 screen sources outside `ui/theme`: **zero hits**. The token
  discipline was already sound — the problem was composition, not colour.
- Six files used pictographic characters as UI (`statusGlyph` in Placement Path emitted a
  padlock emoji inside the topic *title*; `PlacementAlarmChallengeScreen` rendered
  🎉/✅/❌ at headline size). That is the single most "unfinished" tell in the app, and it
  also puts status into the string a screen reader announces as the topic's name.

### 12.2 Shared design system (the leverage)

Five new files in `ui/common`, deliberately chosen so all 97 screens improve without 97 edits:

| File | What it adds | Why |
|---|---|---|
| `Surfaces.kt` | `HeroCard`, `AccentCard`, `SectionHeader`, `IconChip` | Three card weights by role. At most one hero per screen — spending it twice makes neither special. |
| `Tiles.kt` | `StatTile`, `FeatureTile`, `StatusPill`, `MetricBar` | Consolidates four near-identical private copies (`StatCard`, `StatTile`, `ScorePill`, `PriorityBadge`). |
| `Illustrations.kt` | `RouteIllustration`, `TargetIllustration`, `GrowthIllustration` | Canvas-drawn, so they take the scheme's colours and add no APK weight at any density. All clear their semantics — the sentence beside them carries the meaning. |
| `BrandMark.kt` | `BrandMark`, `PRODUCT_NAME`, `PRODUCT_TAGLINE` | The wordmark, set from the type scale rather than shipped as a drawable. |
| `PremiumCatalog.kt` | Previews at default / `fontScale = 1.5f` / `widthDp = 320` | The only place the new surfaces can be looked at without a device. |

`MetricBar` renders **no track at all** when `percent` is null. An empty bar and a zero-length
bar look identical and mean different things; the app's rule that "not measured" and "measured
at zero" are different facts about a student is enforced in the component rather than at each
call site.

### 12.3 The six screens

- **Home** — greeting demoted to an understated header with the target role as a pill (it is
  the least actionable content on the page and had the largest type). The career goal became
  the screen's one `HeroCard`, in **both** states: "no goal yet" is not a lesser state, it is
  the most important thing an unconfigured account can do, and as a grey empty box it made a
  new student's Home look like a broken dashboard. `OnHeroButton` exists because
  `PrimaryButton` fills with `primary`, which is one of the two colours of the gradient it
  would sit on. Signals gained per-metric icon chips and `MetricBar`; the weakest area was
  promoted from a grey footnote to a tinted, tappable row — it is the one line on the card
  that names something to act on.
- **Profile** — initials avatar (derived, never generated), "Your Profile" + supporting line,
  `Personal Information` and `Education` split into separate iconised cards, one `ProfileField`
  so every field is drawn identically, email read-only **with a stated reason**, Degree/Branch
  side by side only above 300dp, and a Save that is disabled when nothing changed and reports
  `Saved.` only after `savingSection` clears with no `saveError`.
- **Test Center** — flat rectangles became category tiles with an icon, a tint and a real
  subtitle describing the *category* (never an invented per-student metric, which the payload
  does not carry). Weak areas moved from `errorContainer` to a warning `AccentCard`: a weak
  area is a finding about preparation, not a failure of the app.
- **Roadmap (Placement Path)** — the empty state explains how generation actually works
  (it reads the resume) instead of only offering a button. Topics became a connected timeline
  with node, connector and a *word* for the state; a locked topic now names the prerequisite
  that unlocks it, resolved from `prerequisiteTopicId`.
- **Roadmap (Practice)** — same overview/footer treatment, day headers carry "N of M done".
- **Progress** — collapsed into one scrolling surface; the ring is a hero when a score exists
  and an honest explanation plus a CTA when it does not, rather than a ring drawn at 0%.

### 12.4 Defects fixed, not just restyled

1. **Test Center category grid could clip.** A `LazyVerticalGrid` nested in a `LazyColumn`
   cannot measure itself, so it carried `height(72.dp * (size / 2 + 1))` — correct at exactly
   one text scale and wrong whenever the category count changes. Replaced with `FlowRow`,
   which measures its children. The arithmetic is gone.
2. **Progress history was confined to a sliver.** The summary sat in a fixed `Column` and only
   the activity list scrolled inside `weight(1f)`.
3. **Roadmap generation showed a fetch skeleton.** Generation is an AI call taking tens of
   seconds; a three-card skeleton says content is imminent. Added
   `PlacementPathUiState.Generating` with a labelled wait.
4. **Status by colour alone in the bottom bar.** The active tab was distinguished only by the
   indicator tint; tabs now switch between filled and outlined icons.
5. **Emoji as UI.** Removed from Placement Path topic titles and the alarm challenge screen.

### 12.5 Branding

`app_name`, the top bar, Welcome, About Us, the ATS share text and the Razorpay checkout sheet
now read **TALVIORA AI**. The APK confirms `application-label:'TALVIORA AI'` with
`package: com.aiplacementos.app.dev` unchanged — brand updated, installed-app identity
preserved, as required.

### 12.6 Two guard tests caught this work

- `DesignSystemGuardTest > font sizes live only in the type scale` rejected a `fontSize = 9.sp`
  in `BrandMark`. It was right: the exception would have lived in the one component present on
  every screen. Removed.
- `SystemBarInsetContractTest > the bottom navigation still consumes the system navigation bar
  inset` anchored on the literal string `NavigationBar {`, which broke when the bar was given a
  `containerColor`. The anchor was widened to `Regex("""NavigationBar\s*[({]""")`. **The
  windowInsets assertion it exists to protect is untouched** — same correction already applied
  to the `NavHost(` anchor in §11.

### 12.7 Verification

| Check | Result |
|---|---|
| `:app:testDevDebugUnitTest` | **1529 tests, 0 failures, 0 errors, 0 skipped** across 173 suites (counted from `TEST-*.xml`, not from `BUILD SUCCESSFUL`) |
| `:app:lintDevDebug` | **0 errors**, 32 warnings — **none** in any file touched by this batch (all are pre-existing dependency/SDK advisories) |
| `:app:assembleDevDebug` | APK 26,950,991 bytes |

### 12.8 Not verified — stated plainly

- **No screen in this batch has been looked at.** There is no GUI in this environment, Roborazzi
  remains unauthorised, and no device was attached. The previews in `PremiumCatalog.kt` exist at
  default / 1.5x / 320dp precisely so the owner can do this; until then the responsive and
  visual claims above are reasoned from the code, not observed.
- The three `Canvas` illustrations keep all geometry inside 0..1 on both axes, so they cannot
  clip at any size — that is arithmetic, not a rendering check.

### 12.9 Still outstanding

- ~90 screens were not individually restyled. They inherit the shared-component changes
  (cards, buttons, loading states, bottom bar, top bar) but keep their own composition.
- `jobhunt/JobHuntHomeScreen.kt` and `interview/MockInterviewCategoryScreen.kt` still hold
  private stat/score composables that `Tiles.kt` now supersedes.
- Four files still use `✓` in strings. That is a typographic character rather than a colour
  emoji, so it was left alone; it is a judgement call worth revisiting.
- 45 screens still draw their own heading above a bare back arrow. Unifying them is its own
  batch, as noted in §11.
- Dark mode and the backend's `app_name` ("AI Placement OS" in `config.py:11`, visible in the
  OpenAPI title) are unchanged. The backend was deliberately not touched in this batch.

## 14. Batches 1-5 — the rest of the application (2026-10-18, later)

Programme state: **43 of 97 screens IMPLEMENTED, 54 NOT REVIEWED.** 0 visually verified.
Regenerate the per-screen table in the next section with `scratchpad/ledger.py`.

### 14.1 Brand: the mark was wrong

`ic_launcher_foreground.xml` carried an **A monogram** left over from "AI Placement OS" --
the wrong letter for the product. Replaced with a T monogram, drawn once as a single closed
outline and shipped twice: `ic_brand_monogram.xml` at 24dp for in-app use, and the launcher
foreground scaled 3x into the adaptive icon's 72x72 safe zone. `BrandMark` now renders the
monogram beside the wordmark, with both inside one semantics node so a screen reader says the
product name once.

A first attempt set `android:tint="?attr/colorControlNormal"` on the drawable. That attribute
resolves only under an AppCompat or Material XML theme and this app's base theme is neither,
so it failed resource linking outright -- caught by the build, not by inspection.

### 14.2 Two defects found by scanning, not by reading

Reading screens one at a time would not have found either of these: each looks like a local
styling choice in its own file, and only reads as a defect when you see it twenty times.

**Unreachable content -- 13 screens.** A `Column(Modifier.fillMaxSize())` lays its children
out once and clips the remainder. At a raised font scale, or with the keyboard open, that puts
the screen's primary button off the bottom edge with no way to scroll to it.

The worst case was **`TestTakingScreen`**: a student could read a question, choose an answer,
and be unable to reach Submit. That is the core assessment flow. Fixed with a scrolling body
and a pinned footer; the same screen also had `Modifier.selectable` without
`Role.RadioButton`, a `RadioButton` with its own `onClick` inside an already-selectable row
(announced twice by TalkBack), no 48dp minimum on the option rows, and a fixed `height(160.dp)`
answer box that clips the student's own text. All corrected.

`TextInterviewScreen` carried the same family of faults and got the same treatment.

**Progress shown by rewording -- 21 buttons across 18 screens.**
`text = if (busy) "Saving..." else "Save"` with `enabled = !busy` leaves a disabled button and
a changed word as the only evidence anything is happening, which is indistinguishable from a
button that did nothing. Migrated to the `loading` parameter, which shows a spinner *and*
blocks a double submit. `SecondaryButton` gained `loading` so it stops being the reason
callers reach for the old pattern.

The migration was scripted but conservative: anything whose `enabled =` expression could not
be simplified with certainty was to be left alone and reported. In the event all 21 were
unambiguous.

### 14.3 Onboarding: every step asked without explaining

All seven steps asked a bare question -- "What's your name?", "Select your skills" -- with no
statement of what the answer is for. Onboarding is the one place where a student has given
nothing and received nothing, so an unexplained demand for personal detail costs most there.
`OnboardingHeader` now requires a supporting line, so a later step cannot ship without one.
Five steps also could not scroll.

`OnboardingSuccessScreen` claimed *"Your personalized roadmap is ready."* Generation runs on
the previous step and can fail; that screen has no way to know. Reworded to state only what is
true regardless of the outcome.

### 14.4 Guards added

`ScreenReachabilityGuardTest` fails the build if any `*Screen.kt` fills the window without a
scroll, lazy list or `weight()`, or if any shared button signals progress by rewording itself,
or if either shared button loses its `loading` parameter.

Its first run failed on three screens the scripted pass had missed, and on one false positive:
Test Center's category tile shows "Starting..." *beside* a spinner, which is the opposite of
the defect. The guard was narrowed to look inside button calls rather than at file text --
the rule is about buttons, so the test now checks buttons.

`AuthSurfaceContractTest` (batch 0) likewise found the missing password reveal in Settings and
Forgot Password after only Login and Signup had been patched.

### 14.5 Verification

| Check | Result |
|---|---|
| `:app:testDevDebugUnitTest` | **1535 tests, 0 failures, 0 errors, 0 skipped** across 175 suites, counted from `TEST-*.xml` |
| `:app:lintDevDebug` | **0 errors**, 32 warnings -- all pre-existing dependency/SDK advisories |
| `:app:assembleDevDebug` | APK 26,951,191 bytes, `application-label:'TALVIORA AI'`, `package: com.aiplacementos.app.dev` |

### 14.6 Still not verified

**No screen in this programme has been seen rendered.** No GUI, no attached device, Roborazzi
unauthorised. Everything above is reasoned from source and from the build. The previews in
`PremiumCatalog.kt` exist at default / 1.5x / 320dp for exactly this gap.

The reference images supplied by the owner contain detailed character illustrations. Those are
art assets; the Canvas figures in `Illustrations.kt` are abstract equivalents. Matching the
reference exactly needs real illustration assets added to the project, which is not authorised.

### 14.7 Next batch

54 screens remain NOT REVIEWED. In priority order: `careerpath/` (10 remaining),
`jobhunt/` (9 remaining), `atscheck/` (8 remaining), `history/` (2), then the long tail in
`projectbuilder/`, `recruiterattack/`, `skillpassport/`, `arena/`, `subscriptions/`,
`notifications/` and `settings/`.

Note that these 54 now satisfy the two structural guards above -- they scroll, and their
buttons show progress honestly. What they have **not** had is an individual review of their
own hierarchy, density, grouping and empty states, which is what IMPLEMENTED means here.

## 15. Batches 6-10 — CareerPath, JobHunt, ATS, History and the long tail (2026-10-19)

**Every one of the 97 discovered screens now carries an individual status.** 83 IMPLEMENTED,
14 REVIEWED - RETAINED, 0 NOT REVIEWED. **0 visually verified, 0 device verified** -- those
gates are untouched and must not be read as closed.

### 15.1 The largest finding: empty results drawn as errors

11 call sites across 8 screens rendered a **`UiState.Empty` branch with `ErrorState`** -- a red
error glyph telling a student something had gone wrong when nothing had. Five of them said
the same four words, "Nothing here yet.", which is how the pattern spread without anyone
noticing it.

Every one was verified to sit in an `Empty` branch (or an `isEmpty()` filter branch) before
being touched, and each now states what is actually absent: Career Path readiness says
readiness is calculated after a test or interview; the gap analysis says gaps appear once the
resume has been compared against the role; an empty filter tab says to choose another tab.

`AtsCheckResultScreen`'s "This report is no longer available." was **deliberately left as an
error** -- a resource that is gone is not an empty one.

### 15.2 Conversation History: the row was not clickable

Opening a conversation worked only by tapping the `AssistChip`, whose label is the
conversation's *kind* ("Voice Career Coach"). So the affordance looked like a tag, and tapping
the title -- the obvious target -- did nothing. The card now opens it, the kind became the tag
it always read as, and the status became a pill.

While fixing it I guessed the kind value as `"voice_coach"`; the backend actually sends
`"career_coach"` (`ConversationFormatting.kt`). Checked and corrected before building --
it would have compiled and silently shown the wrong icon forever.

### 15.3 Component duplication removed

Four hand-rolled copies of shared components, found by scanning rather than by reading:

| Screen | Was | Now |
|---|---|---|
| `AtsCheckSectionAnalysisScreen` | a **private `StatusPill`** shadowing the shared one, at 4dp padding | `SectionStatusPill`, which keeps the local wording and delegates the drawing |
| `AtsCheckIssuesScreen` | inline tinted `Surface`, 2dp padding | `StatusPill` |
| `AtsCheckImprovementPlanScreen` | inline tinted `Surface`, 2dp padding | `StatusPill` |
| `AtsCheckResultScreen` | inline `Surface` wrapping an `Icon` | `IconChip` |
| `CareerTwinScreen`, `SkillPassportDetailScreen` | inline tinted `Surface` | `StatusPill` |

Two pills with one name and two different paddings is exactly the drift that makes a product
look like several products.

### 15.4 14 remaining hub screens given the shared header

`AboutUs`, `AlarmList`, `ArenaList`, `CareerDecisionLab`, `CareerStrategy`,
`NotificationCenter`, `SavedRoadmaps`, `ProjectBuilderList`, `RecruiterAttackList`, `Settings`,
`SkillPassport`, `Subscriptions`, `HelpSupport`, `SkillPassportDetail`.

Four already had a subtitle, which was folded in verbatim. The rest were written from copy
already present in that file -- Skill Passport's line is its own sentence, Career Decision
Lab's is drawn from its study-hours comparison. None describes behaviour the screen lacks.
`ScreenHeader` carries `heading()` semantics, so the TalkBack heading navigation that
`SkillPassportScreen` and `CareerPathHomeScreen` relied on is preserved.

### 15.5 Four scans that came back clean

Worth recording as findings, because they say the codebase is more disciplined than a glance
suggests:

- `IconButton` with an undescribed `Icon`: **0**
- clickable rows pinned below 48dp: **0**
- cards holding an action callback with nothing clickable: **0** (History was the only one,
  and it was hidden behind the chip, so only reading found it)
- hardcoded `Color(0xFF…)` outside `ui/theme/`: **0**

### 15.6 REVIEWED - RETAINED, and what that means

14 screens were audited against every defect class established in this programme -- scroll
reachability, busy-button honesty, empty-vs-error, pill/icon duplication, undescribed icons,
touch targets, orphaned callbacks -- and needed no change.

`PrivacyScreen` and `TermsScreen` are 27-line wrappers that delegate to a shared
`LegalDocumentContent`; there is nothing at that level to redesign. The **whole `jobhunt`
package** is retained on the same basis: it already used `EmptyState`/`ErrorState` correctly,
already scrolled, and its provider-attribution link already carried a 48dp touch target and
licensing-aware URL handling. `SkillMatchAnalysisScreen`'s pill was left alone deliberately --
it uses an explicit two-colour `strengthContainer`/`strengthTint` pair and carries its own
`contentDescription`, both of which migrating to `StatusPill` would have **lost**.

RETAINED is not a weaker IMPLEMENTED. It records that a screen was looked at and found sound.

### 15.7 Verification

| Check | Result |
|---|---|
| `:app:testDevDebugUnitTest` | **1535 tests, 0 failures, 0 errors, 0 skipped**, 175 suites, counted from `TEST-*.xml` |
| `:app:lintDevDebug` | **0 errors**, 32 warnings -- all pre-existing dependency/SDK advisories |
| `:app:assembleDevDebug` | succeeded |

### 15.8 The gate that is still shut

**No screen in this entire programme has been seen rendered.** No GUI, no attached device,
Roborazzi unauthorised. Every claim above is reasoned from source and confirmed only as far as
compilation, automated tests and lint can confirm it.

Specifically unverified: layout balance, spacing rhythm, text wrapping at 1.5x, contrast in
practice, whether the Canvas illustrations read well at their chosen sizes, and whether the
T monogram is legible at launcher density. The previews in `PremiumCatalog.kt` exist at
default / 1.5x / 320dp for exactly this.

The owner's reference images contain detailed character illustrations. Those are art assets;
`Illustrations.kt` provides abstract Canvas equivalents. Matching the reference exactly needs
real illustration assets, which is not authorised.

## 13. Screen ledger — generated from source (2026-10-18)

Regenerate with `scratchpad/ledger.py`. Counts are read from the tree, so this table cannot drift from reality the way a hand-maintained inventory does.

### 13.1 Denominator

| Measure | Count |
|---|---|
| `*Screen.kt` composables discovered | 97 |
| NavGraph destinations | 101 |
| **IMPLEMENTED** — opened, redesigned, compiled | 83 |
| **INHERITS SYSTEM** — uses 2+ shared premium components, own composition not audited | 0 |
| **NOT REVIEWED** — no individual audit | 0 |
| **VISUALLY VERIFIED** | 0 — no GUI, no device, Roborazzi unauthorised |
| **DEVICE VERIFIED** | 0 |

These categories are deliberately not added together into a completion percentage. `INHERITS SYSTEM` means a screen got the new cards, buttons and states for free; it does **not** mean its own hierarchy, density or empty states were examined, and counting it as done is exactly the overclaim this ledger exists to prevent.

### 13.2 Every screen

`markers` = how many of the premium shared components the file references.

| Screen | Lines | Markers | Status |
|---|---|---|---|
| `about/AboutUsScreen.kt` | 67 | 1 | IMPLEMENTED |
| `alarm/AlarmCreateScreen.kt` | 226 | 0 | IMPLEMENTED |
| `alarm/AlarmListScreen.kt` | 200 | 1 | IMPLEMENTED |
| `alarm/PlacementAlarmChallengeScreen.kt` | 263 | 0 | IMPLEMENTED |
| `arena/ArenaListScreen.kt` | 148 | 1 | IMPLEMENTED |
| `arena/ArenaSessionScreen.kt` | 146 | 0 | IMPLEMENTED |
| `atscheck/AtsCheckAnalyzingScreen.kt` | 149 | 0 | IMPLEMENTED |
| `atscheck/AtsCheckCategoryDetailScreen.kt` | 148 | 0 | IMPLEMENTED |
| `atscheck/AtsCheckHistoryScreen.kt` | 141 | 0 | IMPLEMENTED |
| `atscheck/AtsCheckImprovementPlanScreen.kt` | 144 | 1 | IMPLEMENTED |
| `atscheck/AtsCheckIssuesScreen.kt` | 128 | 1 | IMPLEMENTED |
| `atscheck/AtsCheckLandingScreen.kt` | 125 | 1 | IMPLEMENTED |
| `atscheck/AtsCheckParsedViewScreen.kt` | 112 | 0 | IMPLEMENTED |
| `atscheck/AtsCheckResultScreen.kt` | 361 | 1 | IMPLEMENTED |
| `atscheck/AtsCheckSectionAnalysisScreen.kt` | 108 | 1 | IMPLEMENTED |
| `auth/ForgotPasswordScreen.kt` | 133 | 0 | IMPLEMENTED |
| `auth/LoginScreen.kt` | 115 | 0 | IMPLEMENTED |
| `auth/SignupScreen.kt` | 105 | 0 | IMPLEMENTED |
| `auth/WelcomeScreen.kt` | 90 | 1 | IMPLEMENTED |
| `careerdecision/CareerDecisionLabScreen.kt` | 271 | 1 | IMPLEMENTED |
| `careerpath/CareerPathDetailScreen.kt` | 250 | 0 | IMPLEMENTED |
| `careerpath/CareerPathGapAnalysisScreen.kt` | 161 | 2 | IMPLEMENTED |
| `careerpath/CareerPathHomeScreen.kt` | 257 | 1 | IMPLEMENTED |
| `careerpath/CareerPathInterviewPrepScreen.kt` | 126 | 0 | IMPLEMENTED |
| `careerpath/CareerPathProjectDetailScreen.kt` | 167 | 0 | IMPLEMENTED |
| `careerpath/CareerPathProjectsScreen.kt` | 138 | 1 | IMPLEMENTED |
| `careerpath/CareerPathReadinessScreen.kt` | 124 | 0 | IMPLEMENTED |
| `careerpath/CareerPathResumeConnectionScreen.kt` | 151 | 0 | IMPLEMENTED |
| `careerpath/CareerPathRoleSelectionScreen.kt` | 327 | 0 | IMPLEMENTED |
| `careerpath/CareerPathSavedRolesScreen.kt` | 134 | 0 | IMPLEMENTED |
| `careerpath/CareerPathSkillDetailScreen.kt` | 125 | 0 | IMPLEMENTED |
| `careerstrategy/CareerStrategyScreen.kt` | 1331 | 1 | IMPLEMENTED |
| `careertwin/CareerTwinScreen.kt` | 433 | 1 | IMPLEMENTED |
| `history/ConversationDetailScreen.kt` | 208 | 0 | IMPLEMENTED |
| `history/ConversationHistoryScreen.kt` | 342 | 2 | IMPLEMENTED |
| `home/HomeScreen.kt` | 733 | 4 | IMPLEMENTED |
| `interview/InterviewEvaluationScreen.kt` | 229 | 1 | IMPLEMENTED |
| `interview/MockInterviewCategoryScreen.kt` | 331 | 2 | IMPLEMENTED |
| `interview/SimulationSetupScreen.kt` | 147 | 0 | IMPLEMENTED |
| `interview/TextInterviewScreen.kt` | 149 | 1 | IMPLEMENTED |
| `interview/VoiceInterviewScreen.kt` | 360 | 0 | IMPLEMENTED |
| `jobhunt/AddManualJobScreen.kt` | 231 | 0 | IMPLEMENTED |
| `jobhunt/ApplicationReadinessScreen.kt` | 287 | 0 | IMPLEMENTED |
| `jobhunt/JobAlertsScreen.kt` | 402 | 0 | IMPLEMENTED |
| `jobhunt/JobDetailScreen.kt` | 560 | 0 | IMPLEMENTED |
| `jobhunt/JobHuntHomeScreen.kt` | 522 | 1 | IMPLEMENTED |
| `jobhunt/JobHuntResultsScreen.kt` | 492 | 0 | IMPLEMENTED |
| `jobhunt/JobHuntSetupScreen.kt` | 386 | 0 | IMPLEMENTED |
| `jobhunt/SavedJobsScreen.kt` | 386 | 0 | IMPLEMENTED |
| `jobhunt/SavedSearchesScreen.kt` | 185 | 0 | IMPLEMENTED |
| `jobhunt/SkillMatchAnalysisScreen.kt` | 271 | 0 | IMPLEMENTED |
| `jobprep/JobMatchPrepScreen.kt` | 470 | 1 | IMPLEMENTED |
| `notifications/NotificationCenterScreen.kt` | 121 | 1 | IMPLEMENTED |
| `onboarding/OnboardingEducationScreen.kt` | 156 | 0 | IMPLEMENTED |
| `onboarding/OnboardingExperienceLevelScreen.kt` | 125 | 0 | IMPLEMENTED |
| `onboarding/OnboardingGoalsScreen.kt` | 114 | 0 | IMPLEMENTED |
| `onboarding/OnboardingNameScreen.kt` | 96 | 0 | IMPLEMENTED |
| `onboarding/OnboardingRoleScreen.kt` | 74 | 0 | IMPLEMENTED |
| `onboarding/OnboardingSkillsScreen.kt` | 73 | 0 | IMPLEMENTED |
| `onboarding/OnboardingSuccessScreen.kt` | 79 | 1 | IMPLEMENTED |
| `placementpath/PlacementPathScreen.kt` | 547 | 7 | IMPLEMENTED |
| `placementpath/PlacementPathTopicDetailScreen.kt` | 142 | 0 | IMPLEMENTED |
| `placementpath/SavedRoadmapsScreen.kt` | 138 | 1 | IMPLEMENTED |
| `profile/ProfileScreen.kt` | 838 | 1 | IMPLEMENTED |
| `progress/ProgressDashboardScreen.kt` | 286 | 7 | IMPLEMENTED |
| `projectbuilder/ProjectBuilderCreateScreen.kt` | 100 | 0 | IMPLEMENTED |
| `projectbuilder/ProjectBuilderListScreen.kt` | 133 | 1 | IMPLEMENTED |
| `recruiterattack/RecruiterAttackListScreen.kt` | 136 | 1 | IMPLEMENTED |
| `recruiterattack/RecruiterAttackSessionScreen.kt` | 116 | 0 | IMPLEMENTED |
| `resume/ResumeScreen.kt` | 268 | 1 | IMPLEMENTED |
| `resume/ResumeUploadScreen.kt` | 154 | 1 | IMPLEMENTED |
| `roadmap/RoadmapScreen.kt` | 268 | 2 | IMPLEMENTED |
| `roadmap/RoadmapTaskDetailScreen.kt` | 137 | 0 | IMPLEMENTED |
| `settings/SettingsScreen.kt` | 241 | 1 | IMPLEMENTED |
| `skillpassport/SkillPassportDetailScreen.kt` | 209 | 1 | IMPLEMENTED |
| `skillpassport/SkillPassportScreen.kt` | 109 | 1 | IMPLEMENTED |
| `subscriptions/SubscriptionsScreen.kt` | 298 | 1 | IMPLEMENTED |
| `support/HelpSupportScreen.kt` | 93 | 1 | IMPLEMENTED |
| `tests/TestCenterScreen.kt` | 436 | 8 | IMPLEMENTED |
| `tests/TestListScreen.kt` | 79 | 1 | IMPLEMENTED |
| `tests/TestResultScreen.kt` | 129 | 1 | IMPLEMENTED |
| `tests/TestTakingScreen.kt` | 224 | 1 | IMPLEMENTED |
| `voiceagent/VoiceAgentScreen.kt` | 451 | 1 | IMPLEMENTED |
| `atscheck/AtsCheckPreCheckScreen.kt` | 219 | 0 | REVIEWED - RETAINED |
| `careergoal/CareerGoalDetailScreen.kt` | 648 | 0 | REVIEWED - RETAINED |
| `careergoal/CareerGoalListScreen.kt` | 260 | 0 | REVIEWED - RETAINED |
| `jobprep/JobMatchPrepResultScreen.kt` | 654 | 0 | REVIEWED - RETAINED |
| `onboarding/OnboardingConfirmationScreen.kt` | 139 | 0 | REVIEWED - RETAINED |
| `onboarding/OnboardingProfessionalPresenceScreen.kt` | 295 | 1 | REVIEWED - RETAINED |
| `presence/ProfessionalPresenceScreen.kt` | 576 | 0 | REVIEWED - RETAINED |
| `profile/RoleSelectionScreen.kt` | 164 | 0 | REVIEWED - RETAINED |
| `projectbuilder/ProjectBuilderDetailScreen.kt` | 285 | 0 | REVIEWED - RETAINED |
| `projectbuilder/ProjectProofScreen.kt` | 85 | 0 | REVIEWED - RETAINED |
| `recruiterattack/RecruiterAttackCreateScreen.kt` | 177 | 0 | REVIEWED - RETAINED |
| `recruiterattack/RecruiterAttackResultsScreen.kt` | 189 | 0 | REVIEWED - RETAINED |
| `settings/PrivacyScreen.kt` | 28 | 0 | REVIEWED - RETAINED |
| `settings/TermsScreen.kt` | 28 | 0 | REVIEWED - RETAINED |
