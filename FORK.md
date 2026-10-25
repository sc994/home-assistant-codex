# sc994 fork: persistent remote control

This fork of moryoav/home-assistant-codex includes image-level fixes for Codex remote control. The source changes contain no Home Assistant configuration, credentials, or pairing data.

## Images and builds

The worker image is ghcr.io/sc994/codex-cli-worker:0.1.81. Changes to the worker on main trigger the existing multi-architecture build workflow; it can also be run manually under Actions. Enable Actions for the fork if GitHub asks. The workflow uses the repository GITHUB_TOKEN, so a separate registry password is not needed.

Before installation, verify that the image build and multi-architecture manifest succeeded. Ensure the published GHCR package is publicly readable so Home Assistant can pull it without additional registry credentials. Do not install a version before its image is published.

## Installation and migration

Add https://github.com/sc994/home-assistant-codex to the Home Assistant app store repositories. This is a separate app from the upstream installation, with separate private /data.

Keep the existing app installed and create a Home Assistant backup before migration. Stop the old worker before using the new worker, because both write to /config. Migration of private app data must be performed locally through the Home Assistant host, with both apps stopped; never commit or upload authentication, worker tokens, or remote pairing files. Alternatively, configure and sign in to the new app afresh and pair it again. The integration may need to be reconfigured for the new worker, and an inherited worker token must not be shared by simultaneously running workers.

Enable the new app's Start on boot setting if it should start with Home Assistant. Remote control is restored only when /data/codex-home/app-server-daemon/settings.json contains remoteControlEnabled=true. For a fresh installation, sign in first and run codex remote-control start once through an authorized worker task. It saves the preference; later app starts restore it automatically.

## Verification

After the image is installed, verify a connected remote session and execute a sandboxed shell command. Restart the app using Home Assistant, then verify the connection and shell execution again without manual repair commands. Confirm the daemon and updater both use /opt/codex-sandbox on PATH. Full Home Assistant restart persistence also depends on the app's Start on boot setting.

The image includes procps-ng. Its startup explicitly selects the existing Bubblewrap wrapper so the scoped AppArmor transition remains in effect. Remote startup retries up to six times and never blocks the Home Assistant worker. It does not disable AppArmor or change host namespace settings.

Image publishing, installation, migration, and a real restart must each be verified; source tests alone do not establish persistence.

## Remote Home Assistant verification (0.1.77)

After updating and restarting the sc994 app, open a new remote session so Codex discovers `home_assistant_remote.verify`. Existing sessions may need their MCP tools refreshed. Ask it to call `verify` with `{"operation":"logs"}` for Core logs. Supported operations are `entity`, `config_check`, `logs`, `dashboard_readback`, and `dashboard`; dashboard captures need a local path such as `/lovelace/0`. Inspect the returned desktop/mobile `image_paths` before drawing visual conclusions.

The worker installs one marked `[mcp_servers.home_assistant_remote]` table in its private Codex configuration. Existing model, plugin and MCP settings are preserved; conflicting or malformed configuration is left untouched and logged as a setup failure. This managed MCP registration contains no credentials. It is visible to local worker sessions as well as paired remote sessions.

Verification uses an owner-only Unix socket and a fresh boot capability in `/data/remote-verification`, with no Supervisor token passed to Codex. Requests are accepted only while the saved remote-control preference is enabled. The bridge permits at most 24 checks and four dashboard captures per minute, one remote request at a time, and shares the browser execution lock with worker checks. Redacted results and screenshots are private local evidence, retained for seven days, with paths returned to the requesting agent. They are not attached to an add-on UI conversation.

Remote verification does not run service calls or reloads, and rejects `save_pending:true`: use a worker task for edits, per-turn backups, and dashboard saves. Historical data tools are separate and are not added by this change. Disable remote control to revoke remote verification; the capability rotates when the app restarts. The Home Assistant integration must point to this worker for Core logs and authenticated dashboard captures.

## Interactive approval fix (0.1.78)

The worker generates `approval_policy = "on-request"` and `approvals_reviewer = "user"` for interactive sessions and the legacy `ha_auto_review` profile. The sandbox remains `workspace-write`. Unattended `codex exec` commands explicitly override the policy to `never`; the worker's bidirectional app-server already routes requests to the user.

This fixes a 0.1.77 omission: remote MCP tools could be discovered, but `never` automatically rejected tools that needed approval. Update the app, restart it, and create a fresh remote session. Ask for a Core log check and allow that individual tool request. Declining it must leave the tool unexecuted. Existing remote chats may retain their old session policy.

The pinned CLI regression uses a fake local model/MCP server, no real account or Home Assistant credentials, and proves the failure and both human decisions with the generated configuration. See [the official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) for interactive approval policies and MCP tool settings.

## Upstream dashboard fixes (0.1.79)

Merged moryoav/home-assistant-codex main through `6b749ac314131736429f5d74e17f7aba57acd98e` (upstream 0.1.76). This adds bounded dashboard reads and registered frontend-resource handling. Fork remote startup, private verification bridge and interactive human approvals remain in place. The app still uses the sc994 name/image and fork release numbering. The integration code is unchanged; its version is aligned for packaging.

## Remote worker tasks and upstream merge (0.1.80)

Merged upstream main through `e716071` (upstream 0.1.78), including browser permission batches and remembered permissions, isolated dashboard WebSockets, bounded observational condition subscriptions, and mobile capture timing. Fork release numbers remain independent of upstream.

After the 0.1.80 image build succeeds, update/restart the app and open a fresh remote session (or refresh its MCP tools). The existing `home_assistant_remote` server now also exposes `worker_task`:

- `{"operation":"start","prompt":"Back up and update the Viewport automations so their Viewport actions run only while media_player.living_room_living_room_tv is on and select.up_viewport_live_view is available. Preserve other behavior and validate the configuration."}`
- `{"operation":"status","task_id":"ID returned by start"}`
- `{"operation":"continue","task_id":"ID","turn_id":"current_turn_id returned by status","message":"Follow-up instruction"}`
- `{"operation":"cancel","task_id":"ID"}`

Starting or continuing a task authorizes configuration work through the regular task scheduler, using the app's configured execution settings, per-turn backups, and enabled configuration checks. It does not provide arbitrary HTTP requests, credentials, or direct file access. Status is bounded and redacted; it reports results and whether a native permission request needs human review in the add-on web UI. This tool cannot answer native approval requests or manage browser permissions. A queued response is not completion: poll status and inspect the result.

Only chats created through this tool can be read, continued or cancelled remotely. The marker is stored with the task and survives app restarts. A new boot capability is still required after each restart, all calls still require saved remote control to be enabled, and the existing 24-call/minute limit applies. Disabling remote control revokes further bridge calls; it does not automatically cancel tasks already accepted by the scheduler. Use cancel or the add-on UI for those tasks. No new LAN port or Supervisor permission is added.

`verify` remains read-only. For live configuration editing, use `worker_task`; backups and configuration checks remain part of the normal worker run, not the verification bridge.
