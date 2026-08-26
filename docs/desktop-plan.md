# Forge Desktop GTK4 Patch Plan

Forge is moving from a TUI-first multi-provider CLI into a native desktop agent workspace. The implementation should keep the current CLI and Textual UI stable while adding a GTK4/libadwaita frontend over the existing runtime.

## Market Reference

Claude Desktop and Cowork set the useful product target:

- Session sidebar with independent workspaces.
- Provider/model/environment controls near the prompt.
- Chat, plan, tasks, diff, file, preview, and terminal panes.
- Plan-first mode before broad edits.
- Visible subtask progress for parallel agent work.
- Local file access with explicit review and safety states.
- Connectors/extensions/credentials as first-class settings.

## Patch 1 - Native Shell

Status: done.

- Add `forge-desktop` entrypoint.
- Add `desktop.gtk` package.
- Build a libadwaita window with sidebar, provider switcher, transcript, composer, plan pane, tasks pane, and file-change pane.
- Reuse `RuntimeContainer`, `ExecutionService`, `SessionStore`, and existing provider definitions.
- Keep `forge` as the Textual launcher.

## Patch 2 - Workspace Panes

Status: started.

- Convert the plan pane into an actionable plan preview with run/cancel controls.
- Add a real diff/file list from `TaskResult.touched_files`.
- Add artifact viewer for saved run markdown.
- Add model picker and OpenRouter credential dialog.

Implemented in the second slice:

- `Run Plan` toolbar action for the latest generated plan.
- Model dialog for the active provider, with default reset.
- OpenRouter API key dialog backed by the existing credential store.
- Git numstat summaries for touched files in the Diff / Files pane.
- Live `agent_status` events rendered as task rows while orchestrated plans run.
- Artifact pane with run metadata and saved markdown preview.
- `Open Artifact` action for reloading the latest saved run artifact.
- `--help` handling for the GTK module plus a repository-local `./forge-desktop` launcher for Arch/CachyOS systems with externally managed Python.

## Patch 3 - Cowork Mode

- Add project-oriented session grouping.
- Surface orchestration subtasks as active task rows.
- Add steering input for long-running work.
- Add deletion and command-risk confirmations.
- Add scheduled task models and UI placeholders.

## Patch 4 - Polish and Packaging

- Add screenshots and desktop docs to README.
- Add Linux desktop file/app metadata.
- Add packaging notes for system GTK dependencies.
- Add smoke tests for importability and controller behavior.

Visual polish pass started:

- Replace the stacked right inspector with tabbed Plan / Tasks / Diff / Artifact panes.
- Compact the toolbar so provider, model, status, and actions no longer truncate each other.
- Widen and soften the sidebar; align provider labels left.
- Move away from oversized orange controls toward a calmer Claude/Codex-like workspace chrome.
- Default to a centered welcome screen and large prompt card, with provider/model/plan controls inside the composer.
- Hide the inspector until plan/task/diff/artifact content exists.
- Upgrade sidebar from a technical session list to product navigation with recents, current project, and bottom settings.
- Add a prompt hint inside the composer so the empty state reads like a real assistant surface.
- Split composer controls into mode chips and runtime controls, with a neutral local-workspace status chip and compact model chip.
- Make Recents rows open saved run details, touched files, and subtask history from the current session.
- Keep Plan focused on the generated plan pane; Run Plan remains disabled until a real plan exists.
- Turn sidebar navigation into real workspace pages for Search, Providers, Automations, and Settings.
- Convert composer chips into true modes that update hints, active styling, and send behavior.
- Add keyboard shortcuts for Ctrl+Enter send, Ctrl+K search, Ctrl+N new chat, and Escape to close the inspector.
- Improve chat readability with spaced TextBuffer tags and give the inspector a closable production-style header.
- Add a compact context bar above the chat with working directory, mode, provider, model, and busy state.
- Make the composer `+` action insert an `@` context mention instead of behaving like a dead placeholder.
- Put Recents into a scrollable sidebar region so long history does not collapse the rest of the navigation.
- Replace the visible transcript TextView with a message-feed surface using user, assistant, system, event, and error cards while keeping the TextBuffer as a compatibility backing store.
- Validate the GTK CSS provider during polish passes so invalid CSS does not silently degrade the UI.
- Add welcome quick-prompt cards and recent-run cards so the empty state feels like a product start screen rather than a blank canvas.
- Surface provider readiness in the sidebar and Providers page, including warning styling for providers that need setup.
- Replace development-oriented intro copy with a cleaner workspace-ready message.
- Turn the composer `+` control into a real context picker with project-aware `@file` suggestions.
- Surface keyboard shortcuts in Settings so desktop interactions are discoverable.
- Replace the Plan inspector TextView with a structured plan preview: summary card, provider chips, numbered step cards, dependency metadata, and rationale.
- Reset the structured plan preview on new chat so inspector state does not leak between sessions.
- Replace the Diff / Files inspector TextView with file cards, per-file diff stats, and direct open actions.
- Replace the Artifact inspector TextView with a run summary, artifact open action, file switch action, and selectable preview.
- Render Tasks as status cards instead of plain labels so pending, running, success, and failure states scan like a production workspace.
- Convert composer modes to compact icon+label controls and shorten the mode hint so the composer feels closer to Claude/Codex.
- Add an explicit `Accept and run` primary action inside the Plan inspector so generated plans can be approved and executed from the review surface.
- Keep raw run artifact markdown out of the central chat feed; show run metadata in Artifact/Files instead.
- Normalize provider stream events into clean chat status chips without `event:` prefixes or noisy Codex stdin/PATH diagnostics.
- Increase central chat and composer typography for a calmer Claude/Codex-like reading scale.
- Animate new chat messages with GTK revealers and show a live spinner while a provider run is active.
- Add provider accent classes for Qwen, Codex, Claude, and OpenRouter across sidebar providers, composer, context bar, messages, and plan chips.
- Render changed code lines in the Files inspector from unified git diffs, with hunk, added, removed, and context line styling.
- Make the left sidebar and right workspace inspector button-controlled reveal panels so the center workspace can resize cleanly.
- Remove fixed wide composer/page minimums that prevented the desktop from adapting to narrower windows.
- Add the desktop OpenRouter model picker: searchable catalog results, click-to-select model ids, refresh catalog, and CLI-compatible resolver behavior.
- Add local Linux desktop release metadata: app icon, `.desktop` template, metainfo XML, and `scripts/install_desktop.sh`.
- Replace `pip --user` guidance with venv-based installation that works on externally managed Python distributions.
- Replace resizable `Gtk.Paned` side regions with fixed-width overlay drawers so hidden panels do not reserve space and open panels cannot be dragged across the whole window.
- Auto-dismiss both drawers when the prompt gains focus or a request is sent, keeping the composer and main workspace readable while typing.
- Add click-to-dismiss dim backdrops behind drawers and keep left/right panels mutually exclusive so they never overlap the main composer at the same time.
- Clamp drawer widths against the current window width so narrow windows keep a usable visible center strip.
- Stop provider stream events and run-completion updates from auto-opening side drawers; panels update in the background and reveal only from explicit user actions.
- Hide drawer widgets after their slide-out transition so closed panels do not leave translucent overlay remnants.
- Soften motion timing for drawers, stacks, and message revealers.
- Add a warmer layered charcoal visual system with polished popovers, dialogs, composer depth, and less flat black surface treatment.
- Add welcome-page status cards for project, provider, and model so the start screen feels like a live workspace.
- Replace plain empty labels in Plan, Tasks, and Files with styled inspector empty states.
- Add busy styling to the composer and context bar while provider runs are active.
- Restyle the desktop toward the provided Forge concept: persistent left navigation rail, top project/runtime card, orange Forge mark, larger hero surface, card-based quick actions, and accented composer send control.
- Keep the right inspector as a contextual drawer while the left navigation behaves like a stable app rail rather than an overlay panel.
