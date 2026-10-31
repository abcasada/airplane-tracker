# Deploying Airplane Tracker

The app runs as one container plus its own Cloudflare Tunnel container. Nothing is exposed on your router; Cloudflare forwards `https://adsb.<your-domain>` through the tunnel to the app.

## 1. Create the tunnel

1. In the Cloudflare dashboard, go to **Zero Trust → Networks → Tunnels → Create a tunnel** and choose **Cloudflared**.
2. Name it (e.g. `airplane-tracker`). On the "Install connector" step, copy the token from the `docker run` command shown. That long string after `--token` is your `CF_TUNNEL_TOKEN`.
3. Under **Public hostnames**, add:
   - Subdomain: `adsb` (or whatever you like), domain: yours
   - Service: `HTTP`, URL: `airplane-tracker:8080`

   `airplane-tracker` is the compose service name; cloudflared resolves it on the shared Docker network.
4. **Don't** add a Cloudflare Access policy in front of it. The app has its own login with two-factor auth, and Access login redirects break the installed phone app (PWA).

Optional: a free Cloudflare WAF rate-limiting rule on `/api/auth/*` adds a second layer against password guessing.

## 2. Configure and start

```bash
git clone <this repo> airplane-tracker && cd airplane-tracker
cp .env.example .env
# edit .env: CF_TUNNEL_TOKEN and PUBLIC_URL at minimum
docker compose up -d --build
docker compose logs airplane-tracker | grep "setup token"
```

Open `https://adsb.<your-domain>`, enter the setup token from the log, and create your account. You'll scan a QR code with an authenticator app and save 10 recovery codes.

Data lives in the `airplane-data` Docker volume (`/data` in the container): the SQLite database, downloaded aircraft datasets, recordings, and the generated `secret.key`. Back up that volume to keep your places, filters and history.

If you prefer a bind mount (`./data:/data`), make it writable by the container user first: `mkdir data && sudo chown 10001 data`.

## 3. Connect ntfy (phone notifications)

Run your own ntfy server and connect it in **Settings → Notifications (ntfy)**: server URL, topic and a publish token, then **Save & send test notification**. The full walk-through (ntfy's compose file, users and tokens, its own Cloudflare hostname, the phone app) is in **[NTFY.md](NTFY.md)**.

You can also set `NTFY_URL`, `NTFY_TOPIC` and `NTFY_TOKEN` in `.env`. Values from `.env` win over the UI. Set `PUBLIC_URL` too, so notifications can open the app and offer Mute / Not useful buttons.

## 4. Install on your phone

Open the site in Chrome on Android → menu → **Install app** (or **Add to Home screen**). It then opens full-screen like a native app.

## Updating

```bash
git pull
docker compose up -d --build
```

Database migrations run automatically on startup.

## Resource use

One Python process with SQLite and no other services. Expect ~40–150 MB RAM. The compose file caps it at 256 MB.
