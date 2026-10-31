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

## 2. Get the code and start it

You need Docker with the Compose plugin (`docker compose version` should work) and git.

The repository is private until it goes public, so the server needs to authenticate to GitHub once. The simplest way is the GitHub CLI:

```bash
# once: install gh (https://cli.github.com) and log in
gh auth login            # GitHub.com → HTTPS → log in with a browser/code

gh repo clone abcasada/airplane-tracker -- --branch main
cd airplane-tracker
```

(With an SSH key on your GitHub account instead: `git clone -b main git@github.com:abcasada/airplane-tracker.git`.)

Configure and start:

```bash
cp .env.example .env
nano .env                # at least CF_TUNNEL_TOKEN and PUBLIC_URL (https://adsb.<your-domain>)
docker compose up -d --build
docker compose ps        # airplane-tracker should become "healthy" within ~30 s
docker compose logs airplane-tracker | grep -i "setup token"
```

The first build takes a few minutes. On first start the app downloads its datasets (aircraft database, plane-alert-db, airports) in about a minute; the System page shows their status.

Open `https://adsb.<your-domain>`, enter the setup token from the log (or the `SETUP_TOKEN` you put in `.env`), and create your account. You'll scan a QR code with an authenticator app and save 10 recovery codes. Then:

1. **Places:** add your home (or "I'm here now" on your phone), KSLN, and the range (Find FAA airspace → R-3601).
2. **Settings → Notifications (ntfy):** connect your ntfy server and send a test (see section 3).
3. **Filters → + New filter:** start from a template, e.g. "Specific aircraft, anywhere" or "Military visible from where I am".
4. **Live:** the map.

Data lives in the `airplane-data` Docker volume (`/data` in the container): the SQLite database, downloaded aircraft datasets, recordings, and the generated `secret.key`. Back up that volume to keep your places, filters and history.

If you prefer a bind mount (`./data:/data`), make it writable by the container user first: `mkdir data && sudo chown 10001 data`.

## 3. Connect ntfy (phone notifications)

Run your own ntfy server and connect it in **Settings → Notifications (ntfy)**: server URL, topic and a publish token, then **Save & send test notification**. The full walk-through (ntfy's compose file, users and tokens, its own Cloudflare hostname, the phone app) is in **[NTFY.md](NTFY.md)**.

You can also set `NTFY_URL`, `NTFY_TOPIC` and `NTFY_TOKEN` in `.env`. Values from `.env` win over the UI. Set `PUBLIC_URL` too, so notifications can open the app and offer Mute / Not useful buttons.

## 4. Install on your phone

Open the site in Chrome on Android → menu → **Install app** (or **Add to Home screen**). It then opens full-screen like a native app.

## Updating

```bash
cd airplane-tracker
git pull
docker compose up -d --build
```

Database migrations run automatically on startup.

## Resource use

One Python process with SQLite and no other services. Expect ~40–150 MB RAM. The compose file caps it at 256 MB.
