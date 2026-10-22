# sc994 fork: persistent remote control

This fork of moryoav/home-assistant-codex includes image-level fixes for Codex remote control. The source changes contain no Home Assistant configuration, credentials, or pairing data.

## Images and builds

The worker image is ghcr.io/sc994/codex-cli-worker:0.1.76. Changes to the worker on main trigger the existing multi-architecture build workflow; it can also be run manually under Actions. Enable Actions for the fork if GitHub asks. The workflow uses the repository GITHUB_TOKEN, so a separate registry password is not needed.

Before installation, verify that the image build and multi-architecture manifest succeeded. Ensure the published GHCR package is publicly readable so Home Assistant can pull it without additional registry credentials. Do not install a version before its image is published.

## Installation and migration

Add https://github.com/sc994/home-assistant-codex to the Home Assistant app store repositories. This is a separate app from the upstream installation, with separate private /data.

Keep the existing app installed and create a Home Assistant backup before migration. Stop the old worker before using the new worker, because both write to /config. Migration of private app data must be performed locally through the Home Assistant host, with both apps stopped; never commit or upload authentication, worker tokens, or remote pairing files. Alternatively, configure and sign in to the new app afresh and pair it again. The integration may need to be reconfigured for the new worker, and an inherited worker token must not be shared by simultaneously running workers.

Enable the new app's Start on boot setting if it should start with Home Assistant. Remote control is restored only when /data/codex-home/app-server-daemon/settings.json contains remoteControlEnabled=true. For a fresh installation, sign in first and run codex remote-control start once through an authorized worker task. It saves the preference; later app starts restore it automatically.

## Verification

After the image is installed, verify a connected remote session and execute a sandboxed shell command. Restart the app using Home Assistant, then verify the connection and shell execution again without manual repair commands. Confirm the daemon and updater both use /opt/codex-sandbox on PATH. Full Home Assistant restart persistence also depends on the app's Start on boot setting.

The image includes procps-ng. Its startup explicitly selects the existing Bubblewrap wrapper so the scoped AppArmor transition remains in effect. Remote startup retries up to six times and never blocks the Home Assistant worker. It does not disable AppArmor or change host namespace settings.

Image publishing, installation, migration, and a real restart must each be verified; source tests alone do not establish persistence.
