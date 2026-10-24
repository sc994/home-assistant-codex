# sc994 fork: persistent remote control

This fork of moryoav/home-assistant-codex includes image-level fixes for Codex remote control. The source changes contain no Home Assistant configuration, credentials, or pairing data.

## Images and builds

The worker image is ghcr.io/sc994/codex-cli-worker:0.1.78. Changes to the worker on main trigger the existing multi-architecture build workflow; it can also be run manually under Actions. Enable Actions for the fork if GitHub asks. The workflow uses the repository GITHUB_TOKEN, so a separate registry password is not needed.

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
