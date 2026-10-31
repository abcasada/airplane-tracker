# Notifications with ntfy

Airplane Tracker sends phone notifications through your own [ntfy](https://ntfy.sh) server. You run
ntfy separately (its own container and Cloudflare Tunnel hostname); the app only needs its URL, a
topic and a token.

## 1. Run ntfy

`server.yml` (mount it at `/etc/ntfy/server.yml`):

```yaml
base-url: "https://ntfy.example.com"   # your public hostname
behind-proxy: true                      # Cloudflare Tunnel in front
cache-file: "/var/cache/ntfy/cache.db"  # phones fetch what they missed, survives restarts
cache-duration: "24h"
auth-file: "/var/lib/ntfy/user.db"
auth-default-access: "deny-all"         # private: nothing without a login or token
attachment-cache-dir: "/var/cache/ntfy/attachments"
```

`compose.yaml` for ntfy (separate from this app):

```yaml
services:
  ntfy:
    image: binwiederhier/ntfy:latest    # pin a version ≥ 2.16 (needed to update notifications in place)
    command: serve
    restart: unless-stopped
    volumes:
      - ./server.yml:/etc/ntfy/server.yml:ro
      - ntfy-cache:/var/cache/ntfy
      - ntfy-lib:/var/lib/ntfy
    networks: [ntfy]
  cloudflared:
    image: cloudflare/cloudflared:2026.9.3
    restart: unless-stopped
    command: tunnel --no-autoupdate run
    environment:
      TUNNEL_TOKEN: ${CF_TUNNEL_TOKEN}
    networks: [ntfy]
networks:
  ntfy: {name: ntfy}
volumes:
  ntfy-cache: {}
  ntfy-lib: {}
```

In Cloudflare Zero Trust, give that tunnel a public hostname (e.g. `ntfy.example.com`) pointing to
`http://ntfy:80`. Don't put Cloudflare Access in front of it: the phone app can't log in through it,
so ntfy's own accounts and tokens protect it instead. ntfy keeps its WebSocket alive every 45 s,
under Cloudflare's ~100 s idle limit.

## 2. Users, topic and tokens

```bash
docker compose exec ntfy ntfy user add you                 # your phone logs in as this user
docker compose exec ntfy ntfy user add airplane-tracker    # the app publishes as this one
docker compose exec ntfy ntfy access you 'planes-*' read-only
docker compose exec ntfy ntfy access airplane-tracker 'planes-*' write-only
docker compose exec ntfy ntfy token add airplane-tracker   # prints tk_… for the app
```

Pick a topic name that's hard to guess, e.g. `planes-k3v9q2m7x1` (the Settings page suggests one).

## 3. Connect the app

Settings → Notifications (ntfy), or `.env`:

- **URL:** `https://ntfy.example.com`. If both stacks share the `ntfy` Docker network you can
  use `http://ntfy` instead (add the network to this app's compose file as an external network).
- **Topic:** the name you picked.
- **Token:** the `tk_…` from step 2.
- **`PUBLIC_URL`** in `.env` must be set: notifications use it for "tap to open" and for the
  Mute 1 day / Not useful buttons. Those buttons call the app with a signed, single-purpose link
  that expires after a day, so they work without being logged in on the phone.

"Save & send test notification" checks the server and sends a test message.

## 4. Phone

1. Install **ntfy** from Google Play or F-Droid.
2. Settings → Users: add `https://ntfy.example.com` with your user (`you`) and password.
3. Subscribe to your topic on that server.
4. Turn on **instant delivery** for the subscription. A self-hosted server doesn't use Google's push
   service, so this keeps a connection open; battery use is small.
5. Android Settings → Apps → ntfy → Notifications: each ntfy priority has its own channel. Set
   sound/vibration per level, and allow "Max priority" to override Do Not Disturb if you want
   urgent filters to ring at night.

## How filters map to ntfy

| Filter setting | ntfy |
|---|---|
| Priority low / normal / high / urgent | 2 / 3 / 4 / 5 |
| Title and message templates | title and message |
| Military | 🪖 tag |
| Photo | attachment link (plane-alert-db photos) |
| ETA | "ETA 14:32" first line; the notification is replaced in place when it moves |
| One notification per aircraft | `sequence_id` = `aircraft-<hex>`: later alerts replace it |
| Buttons | Open · Mute 1 day · Not useful |

Nothing sent to ntfy includes your home coordinates.
