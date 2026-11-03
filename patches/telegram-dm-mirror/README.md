# Hub and Telegram Plugin Patch: One DM Conversation in Web Chat and Telegram

This folder contains two Go source patches against Scion commit `659cc58f` (nightly `20261022`):

- `scion-hub-telegram-dm-mirror.patch`: the hub side of the mirror.
- `scion-telegram-plugin-hub-api-url+dm-mirror.patch`: the Telegram plugin side of the mirror, plus the `hub_api_url` option for hubs behind IAP.

Tested on nightly `20261022` with Go 1.26.1: the plugin tests, the targeted hub tests and the full `pkg/hub` suite pass.

Apply both. The hub part is gated by the experiment `hub.telegram_dm_mirror`, which is off by default. While it is off, the hub behaves like stock Scion.

## What This Patch Fixes

1. **Telegram plugin behind IAP (`hub_api_url`):**
   - The stock plugin sends its hub API calls (`/api/v1/broker/inbound`, `/api/v1/telegram/link`, project and agent lists) to the public `hub_url`. Behind IAP, these HMAC-signed calls get `401`, so registration, `/setup`, agent lists and message delivery fail.
   - The patch adds the optional plugin config key `hub_api_url`. Set it to `http://localhost:8080` on a single-node hub. API calls then go to the hub directly. Links shown to users still use `hub_url`.
   - The value must use `https://`, or `http://` on a loopback host (`localhost`, `127.0.0.1` or `::1`). Any other value is a configuration error, and the plugin does not start. Thus signed requests and message text never travel unencrypted to another host.
   - Without this key, the plugin behaves like stock.
2. **One DM conversation in both interfaces (`hub.telegram_dm_mirror`):** stock Scion stores one channel per message. Thus web chat and Telegram show different halves of the same DM. The web DM view shows only `channel = web` rows, so Telegram messages are hidden there. Telegram gets only `channel = telegram` rows, so `conv:` replies and web-typed messages never arrive there. With the experiment on:
   - The web DM shows the user's Telegram messages, in history and live. The DM does not become unread because of the user's own Telegram message.
   - Agent replies stored as web messages and messages the user types in web chat are copied to the user's Telegram chat. Web-typed messages show as `👤 <name> (web) → 🤖 <agent>`.
   - The plugin sends a copy only to the chat where the user last talked to that agent on Telegram. Agent conversations happen in groups linked with `/setup`, so this chat is a group. The group must not be public (no `@username`), and its only members must be the user and the bot. The plugin checks group membership, member count and whether the group is public through the Telegram API. It caches the result for 2 minutes, or for 30 seconds after an error. If there is no such chat, or if a check fails, the plugin sends nothing. It never posts copies to other groups linked to the project.
   - The copies are observer-only messages on the agent topic, so the hub stores no extra rows and nothing loops back. Discord rows and notices stay out of the web DM, as before.
   - Agents can reply with `scion message conv:<id>` or `scion message user:<email>`. Both reach both interfaces.
3. **Plugin routing fix:** user-topic replies take the agent slug from the sender, so the plugin finds the user's conversation context instead of broadcasting.

## How to Build and Deploy

```bash
# 1. Create a detached worktree at 659cc58f in your Scion checkout and apply both patches
git -C /path/to/scion worktree add --detach /tmp/scion-tgmirror 659cc58f
cd /tmp/scion-tgmirror
git apply /path/to/scion-defaults/patches/telegram-dm-mirror/scion-hub-telegram-dm-mirror.patch
git apply /path/to/scion-defaults/patches/telegram-dm-mirror/scion-telegram-plugin-hub-api-url+dm-mirror.patch

# 2. Run the tests (compiling the pkg/hub test binary needs more than 13 GB of RAM)
(cd extras/scion-telegram && go test ./internal/telegram/)
go test ./pkg/experiments/ ./pkg/eventbus/ ./pkg/store/entadapter/
go test ./pkg/hub/ -run 'TestTelegramDMMirror|TestHandleBrokerInbound|TestChatV2_|TestG3_|TestPersistedRowEffectEnumeration|TestTestAdminPolicy'

# 3. Build the web UI (the hub binary embeds it; Node 24), the hub and the plugin for Linux amd64.
#    Leave version.Version empty: the binary reports "dev", so the update check never offers
#    a nightly that would replace the patched binary.
(cd web && npm ci && npm run build)
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -ldflags "\
  -X github.com/GoogleCloudPlatform/scion/pkg/version.Commit=$(git rev-parse HEAD) \
  -X github.com/GoogleCloudPlatform/scion/pkg/version.BuildTime=$(date -u +%FT%TZ)" \
  -o /tmp/scion ./cmd/scion
(cd extras/scion-telegram && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  go build -o /tmp/scion-plugin-telegram ./cmd/scion-plugin-telegram)
git -C /path/to/scion worktree remove --force /tmp/scion-tgmirror

# 4. Install on the Hub VM. Keep backups outside plugins/broker/: the hub loads every
#    scion-plugin-* file in that folder as a plugin.
source /path/to/scion-defaults/config/factory.env
HUB_PROJECT_ID="${HUB_PROJECT_ID:-$HUB_PROJECT}"
gcloud compute scp /tmp/scion /tmp/scion-plugin-telegram "${HUB_VM}:/tmp/" --project "$HUB_PROJECT_ID" --zone "$HUB_ZONE"
gcloud compute ssh "$HUB_VM" --project "$HUB_PROJECT_ID" --zone "$HUB_ZONE" --command '
  set -e
  P=/home/scion/.scion/plugins/broker/scion-plugin-telegram
  B=/home/scion/.scion/plugin-backups
  sudo test -e /usr/local/bin/scion.stock || sudo cp -p /usr/local/bin/scion /usr/local/bin/scion.stock
  sudo -u scion mkdir -p "$B"
  sudo test -e "$B/scion-plugin-telegram.stock" || sudo cp -p "$P" "$B/scion-plugin-telegram.stock"
  sudo install -m 755 /tmp/scion /usr/local/bin/scion
  sudo install -o scion -g scion -m 755 /tmp/scion-plugin-telegram "$P"
  rm -f /tmp/scion /tmp/scion-plugin-telegram
  sudo systemctl restart scion-hub'
```

The restart does not stop agent containers.

## Set Up the Telegram Plugin

Follow the official tutorial, [Setting Up Telegram](https://googlecloudplatform.github.io/scion/getting-started/telegram/). It creates the bot, configures the plugin in **Admin → Integrations**, links your account with `/register` and links a group with `/setup`. The [plugin README](https://github.com/GoogleCloudPlatform/scion/tree/main/extras/scion-telegram) covers source builds, `settings.yaml` and webhook mode.

The tutorial is for a local Hub (Workstation mode). On a Hub VM behind IAP, also do these steps:

1. **Install the plugin binary.** Scion's `scripts/single-node-vm/deploy.sh` installs it in `/home/scion/.scion/plugins/broker/` when `chat_plugins` includes `telegram`. Step 4 of [How to Build and Deploy](#how-to-build-and-deploy) puts the patched binary in the same place.
2. **Put the binary on `PATH`.** The **Install** button needs the binary on `PATH` when the Hub has no source checkout:

   ```bash
   sudo ln -sf /home/scion/.scion/plugins/broker/scion-plugin-telegram /usr/local/bin/scion-plugin-telegram
   ```

3. **Set `hub_api_url`.** **Install** writes the plugin config file, `/home/scion/.scion/scion-telegram.yaml`. Add `hub_api_url: http://localhost:8080` to it. Without this key, every plugin call to the Hub gets `401` from IAP.
4. **Restart the Hub once after the first Install.** If the Hub started without a message broker, the plugin does not connect to the message stream. Then the status shows `subscriptions: 0`, and the bot ignores messages. Run `sudo systemctl restart scion-hub` to fix this.
5. **Tell agents to reply with `scion message`.** Only messages that an agent sends with `scion message` reach Telegram. Without the mirror, use `scion message user:<email>`, because `conv:<id>` replies reach only web chat. With the mirror on, both work.

## How to Turn It On

1. Set up the Telegram plugin as described in [Set Up the Telegram Plugin](#set-up-the-telegram-plugin). Link a group that is not public and contains only the user and the bot. Then the user talks to the agent in that group once. Copies go to the chat where the user last talked to that agent.
2. Test with the experiment off first. The hub must behave like stock.
3. Turn on **Admin → Experiments → `hub.telegram_dm_mirror`**. The hub reads it on every message, so no restart is necessary.

## Rollback

- **Soft:** turn off `hub.telegram_dm_mirror`. The hub is stock again immediately. The plugin ignores mirror copies when the hub sends none.
- **Hard:** reinstall `/usr/local/bin/scion.stock` and `plugin-backups/scion-plugin-telegram.stock`, then restart `scion-hub`. The patch changes no schema, and a stock hub ignores the stored experiment value.

## Safety

- **One switch for the whole hub:** the experiment has no per-user opt-in. Every user who links Telegram gets copies.
- **Data leaves the hub:** when the experiment is on, the hub sends web DM text, and files attached to agent replies, to Telegram's servers. Telegram does not end-to-end encrypt bot chats or group chats. Do not turn the experiment on if users can type secrets in web chat, or if agents can print or attach secrets.
- **Who sees copies:** copies go only to a group that is not public and has only the user and the bot as members. If another person joins that group, or the group is made public, copies stop within 2 minutes. Copies already posted stay in the group. A person who joins later can read them if the group shows its history to new members. Anyone can read them once the group is public.
- **`hub_api_url`:** the plugin accepts only `https://` or loopback `http://`. The plugin still signs every call, and the hub still checks the signature. Thus pointing `hub_api_url` at the hub past IAP does not open the hub.

## Limits

- **Forum-topic groups are not supported.** Use a normal group.
- **Delivery:** copies to Telegram are fire-and-forget, so a Telegram rate limit can drop one. Edits and deletes are not copied. Attachments on messages typed in web chat are not copied. The first file attached to an agent reply is sent to the Telegram chat, with the reply text as its caption.
- **Search:** web chat search does not find Telegram messages.
- **Legacy plugin broker:** with `SCION_TELEGRAM_V1=1` the plugin drops all mirror copies.
- **Standalone mode:** `hub_api_url` is read from the plugin config file only, not from `TELEGRAM_*` environment variables.
- **Upgrades:** the patched hub reports `dev`, so it shows no update banner. Re-apply both patches when you move to a newer nightly.
