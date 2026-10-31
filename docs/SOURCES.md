# Data sources: plan and sign-up checklist

Status: **proposed** (2026-10-31). Nothing below is built yet except adsb.fi, adsb.lol, the
enrichment datasets and the FAA airspace search. Terms and prices were checked on 2026-10-31 and
change often; re-check each before signing up.

Secrets (keys, tokens, passwords) go in `.env` or the `/data` volume, never in the repo. Every
source can be turned on/off and shows its status, budget use and errors on the System page.

## How the sources fit together

| Role | What it does | Sources |
|---|---|---|
| **Live, polled** | Positions around your places, every few seconds | Your receiver, adsb.fi, adsb.lol, airplanes.live, FlyItalyADSB, OpenSky |
| **Live, on demand (paid)** | Only for aircraft a filter cares about: fill gaps when free feeds lose them, and answer "where is it going" | Flightradar24, ADS-B Exchange (optional) |
| **Schedule** | Filed flight plans hours ahead (civil IFR only) | FAA SWIM SCDS, FlightAware AeroAPI |
| **Enrichment** | Who/what the aircraft is, routes, photos | tar1090-db, plane-alert-db, FAA aircraft registry, hexdb.io, adsb.im, adsbdb, VRS standing data, Planespotters |
| **Places** | Airports, airspace, elevation | OurAirports, FAA ADDS airspace, USGS elevation |

All live sources feed one merge step keyed by ICAO hex: the freshest position wins, and each
aircraft remembers which sources saw it. That gives two new filter fields (`sources`,
`seen_by_my_receiver`) and a **Sources report**: for each source, how many aircraft it saw that no
other source did, over the last day/week. That report is how you decide whether a paid source is
worth keeping.

## Free live sources

### 1. Your own receiver (planned hardware)

- **Gives:** the best low-altitude coverage near home (pattern traffic, helicopters, low military),
  1-second updates, no rate limits, no terms.
- **Hardware:** an RTL-SDR dongle for 1090 MHz (e.g. FlightAware Pro Stick Plus, which has a
  built-in filter and amplifier, or an RTL-SDR Blog V4) and an outdoor 1090 antenna mounted as high
  as practical. Optional second dongle + 978 MHz antenna for **UAT**: in the US, many GA aircraft
  below 18,000 ft broadcast only on 978, and 1090-only receivers never see them.
- **Software:** the `sdr-enthusiasts/docker-adsb-ultrafeeder` container (readsb + tar1090 map +
  MLAT), plus `dump978` if you add UAT. The app reads its `aircraft.json` over the Docker network
  every second.
- **Feed the aggregators from it.** Ultrafeeder can feed adsb.fi, adsb.lol, airplanes.live,
  ADS-B Exchange, FlyItalyADSB and OpenSky; FR24, FlightAware and RadarBox have their own small
  feeder containers. Feeding unlocks perks you'd otherwise pay for or can't get:

  | Feed to | Feeder perk |
  |---|---|
  | adsb.fi | `/v2/snapshot` endpoint (unlocked by your IP) |
  | adsb.lol | `re-api` access by IP now; the API key they plan to require is "obtained by feeding" |
  | airplanes.live | likely the way to get API access (it currently answers 403 "contact us") |
  | OpenSky | 8,000 API credits/day instead of 4,000 (needs ≥30% uptime that month) |
  | FlyItalyADSB | Pro limits free (300 req/min, whole world), non-commercial |
  | FlightAware | free Enterprise website account; AeroAPI free allowance $10/month instead of $5 |
  | Flightradar24 | free Business app subscription (not API credits) |
  | ADS-B Exchange | only rare reduced-fee exceptions now |

- **Your part:** buy the hardware, decide where it lives (USB into this server, or a Raspberry Pi
  near the antenna), and tell me the feeder accounts you create.

### 2. adsb.fi (built, primary)

- No key. 1 request/s public limit (we use ≤0.5/s), ≤250 nm radius. Personal, non-commercial;
  must link adsb.fi (done on the Live and System pages). Feeders get `/v2/snapshot`.
- **Your part:** nothing. Optionally feed it.

### 3. adsb.lol (built, failover → becomes a merged source)

- No key today; rate limits "dynamic"; ODbL. A key "obtained by feeding" is announced, undated.
- **Your part:** nothing now. Feed it once your receiver runs.

### 4. airplanes.live

- Same JSON format as adsb.fi. Its API page describes access for "contributors, developers and
  customers", personal non-commercial use. It returned 403 "Please contact us" to us on 2026-10-14,
  so per our rules it stays off until they approve.
- **Your part:** either feed it from your receiver, or email them (they ask to be contacted) and
  ask whether a personal, non-commercial alert app polling ≤0.5 req/s around a few places is OK.

### 5. FlyItalyADSB

- Free key by email, no receiver needed. Free tier: 30 req/min, 300,000/month, 500 km radius;
  commercial use allowed, attribution one line (CC BY-SA 4.0). ~3,200 receivers, unfiltered
  (military included), ADS-B Exchange v2 format, so it plugs into our existing parser.
- Coverage is mostly Europe and North America; how much it adds around Salina will show in the
  Sources report.
- **Your part:** request a free key at flyitalyadsb.com → `FLYITALY_API_KEY`.

### 6. OpenSky Network

- Free account. OAuth2 client credentials only (basic auth was retired). 4,000 credits/day
  (8,000 as an active feeder); a regional `/states/all` box costs 1–4 credits, so at 1 credit per
  call that's one poll every ~22 s (every ~11 s as a feeder). No registration/type in the
  data; non-commercial; its own state-vector format (new parser).
- Best used as a slower extra source and for its own receiver network's gaps.
- **Your part:** create an account at opensky-network.org, then create an API client in your
  account page → `OPENSKY_CLIENT_ID`, `OPENSKY_CLIENT_SECRET`.

### Not planned

- **ADSB One:** API archived in 2026 and moved under airplanes.live.
- **ADS-B Hub, RadarBox, Plane Finder:** no public free API for this use (RadarBox's API is paid
  and enterprise-priced). Feeding RadarBox from your receiver is still possible.

## Paid live sources

These are **never polled continuously**: credits would run out in days. They're used only for
aircraft a filter is watching or predicting, when the free feeds have lost them, plus on-demand
lookups from the aircraft page. Each has a monthly budget cap in the app, a credit meter, and
stops cleanly at the cap. Responses are cached and never exported or committed.

### Flightradar24 API (recommended first)

- Plans (check the live price page): **Explorer ~$9/month** (30,000 credits, ~10 req/min; FR24's
  blog has mentioned double credits during promotions), Essential ~$90/month, Advanced ~$900
  (commercial). Credits per call aren't published per endpoint; we'll measure with `/api/usage`.
- **Adds:** origin/destination and flight info for airline and many bizjet flights, FR24's very
  large receiver network, and positions where community feeds have gaps.
- **Doesn't add much for military:** FR24 filters aircraft whose operators ask to be hidden, which
  includes much military traffic. The community aggregators are unfiltered.
- **Your part:** subscribe to Explorer at fr24api.flightradar24.com → `FR24_API_TOKEN`; pick a
  monthly cap.

### ADS-B Exchange (optional)

- Personal plan on RapidAPI: **$10/month for 10,000 requests** (about one request every 4–5
  minutes if spread evenly), ≤250 nm, non-commercial. Enterprise is quote-only.
- Since 2023 most hobby feeders also feed adsb.fi/adsb.lol/airplanes.live, so ADSBx rarely sees
  something those don't. **Recommendation:** skip unless the Sources report shows gaps FR24 doesn't
  fill.
- **Your part (if wanted):** subscribe on RapidAPI → `ADSBX_RAPIDAPI_KEY`.

## Schedule sources (flight plans, hours ahead)

### FAA SWIM SCDS (free)

- **Yes, individuals can sign up.** SCDS is for users outside the FAA who need non-sensitive,
  non-operational data; the FAA lists apps built on it as a valid use. Approval is automatic once
  you accept the terms. Not for operational or safety-critical use (fine for this app).
- **Use it or lose it:** subscriptions are disabled after 60 days of inactivity (the app stays
  connected, so that's covered).
- **Adds:** filed flight plans and flight status for US IFR flights, typically many hours before
  departure: "a C-17 has filed into KSLN for 14:20" style alerts. Sensitive flights (most military)
  are reportedly excluded.
- **Cost to the app:** needs Solace's JMS client library (`solace-pubsubplus`, a native library of
  tens of MB) and processes a national stream locally (modest CPU/RAM, still one process).
- **Your part:** register at data.faa.gov, then in the SWIFT portal (portal.swim.faa.gov) create an
  SCDS subscription to **TFMS Flight Data (TFMData)**. You'll get a Solace host, message VPN,
  username, password and queue name → `SWIM_HOST`, `SWIM_VPN`, `SWIM_USERNAME`, `SWIM_PASSWORD`,
  `SWIM_QUEUE`. Questions: scds@faa.gov.

### FlightAware AeroAPI (Personal; free allowance)

- Personal tier: pay per query, **first $5/month free ($10 for feeders)**, card required. Used only
  for candidate aircraft: filed route, origin/destination, scheduled times. Personal/academic use.
- **Your part:** create a FlightAware account, open AeroAPI → Personal, add a key →
  `AEROAPI_KEY`; the app caps spending at the free allowance unless you raise it.

## Enrichment (no keys)

| Source | Adds | Status |
|---|---|---|
| tar1090-db | type, registration, military flags/ranges | built |
| plane-alert-db (ODbL) | notable aircraft, categories, photo links | built |
| VRS standing data (CC0) | airlines, operators | built |
| adsb.im routeset | routes by callsign | built |
| Planespotters.net | photos (web UI only, with credit) | built |
| **FAA Releasable Aircraft Registry** | N-number owner, type, engine, year; daily bulk file, public domain | planned |
| **hexdb.io** | aircraft info and route by callsign, as a second route source | planned |
| **adsbdb** | routes, third fallback (cached privately, never exported) | planned |

## Order I'd build them

1. **Multi-source merge** + Sources report, then FlyItalyADSB and OpenSky (you only need two free
   keys).
2. **Local receiver** source, as soon as your hardware runs (no keys).
3. **FR24** gap-fill and lookups with the budget cap.
4. **FAA registry + hexdb.io** enrichment.
5. **FAA SWIM** schedule source and `filed_arrival` facts (after you approve the Solace library).
6. airplanes.live, AeroAPI, ADS-B Exchange when you have access and want them.
