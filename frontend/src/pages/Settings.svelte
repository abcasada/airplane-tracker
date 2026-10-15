<script lang="ts">
  import { onMount } from 'svelte'
  import { del, errorMessage, get, post, put } from '../lib/api'
  import { fmtTime } from '../lib/format'
  import { loggedOut, session } from '../lib/session.svelte'
  import { toast } from '../lib/toast.svelte'
  import type { PredictionSettings } from '../lib/types'

  interface HAState {
    url: string
    service: string | null
    token_set: boolean
    from_env: { url: boolean; token: boolean; service: boolean }
  }

  let ha = $state<HAState | null>(null)
  let haUrl = $state('')
  let haToken = $state('')
  let haService = $state<string | null>(null)
  let services = $state<string[]>([])
  let testResult = $state<{ ok: boolean; message?: string; error?: string } | null>(null)
  let busy = $state(false)

  interface Alerting {
    episode_end_after_s: number
    daily_ceiling: number
    repeat_suppress_min: number | null
    quiet_hours: { start: string; end: string } | null
    timezone: string
  }
  interface Exclusion {
    id: number
    filter_id: number | null
    kind: string
    value: string
    until: number | null
    note: string
  }
  let alerting = $state<Alerting | null>(null)
  let prediction = $state<PredictionSettings | null>(null)
  let exclusions = $state<Exclusion[]>([])
  const deviceTz = Intl.DateTimeFormat().resolvedOptions().timeZone

  let pwCurrent = $state('')
  let pwNew = $state('')
  let newCodes = $state<string[]>([])

  onMount(async () => {
    ha = await get<HAState>('/api/settings/ha')
    haUrl = ha.url || 'http://host.docker.internal:8123'
    haService = ha.service
    if (haService) services = [haService]
    alerting = await get<Alerting>('/api/settings/alerting')
    prediction = await get<PredictionSettings>('/api/settings/prediction')
    exclusions = await get<Exclusion[]>('/api/exclusions')
  })

  async function saveAlerting() {
    if (!alerting) return
    try {
      alerting = await put<Alerting>('/api/settings/alerting', $state.snapshot(alerting))
      toast('Alerting settings saved')
    } catch (e) {
      toast(errorMessage(e), 'error')
    }
  }

  async function savePrediction() {
    if (!prediction) return
    try {
      prediction = await put<PredictionSettings>('/api/settings/prediction', $state.snapshot(prediction))
      toast('Prediction settings saved')
    } catch (e) {
      toast(errorMessage(e), 'error')
    }
  }

  let restoreFile = $state<{ name: string; data: Record<string, unknown[] | unknown> } | null>(null)
  let restoreSettings = $state(true)
  let restoring = $state(false)

  async function pickBackup(e: Event) {
    const input = e.currentTarget as HTMLInputElement
    const file = input.files?.[0]
    input.value = ''
    if (!file) return
    try {
      const data = JSON.parse(await file.text())
      if (data?.format !== 'airplane-tracker-backup/1') throw new Error('Not an Airplane Tracker backup file')
      restoreFile = { name: file.name, data }
    } catch (err) {
      toast(errorMessage(err), 'error')
    }
  }

  function count(key: string): number {
    const v = restoreFile?.data[key]
    return Array.isArray(v) ? v.length : 0
  }

  async function doRestore() {
    if (!restoreFile) return
    restoring = true
    try {
      const r = await post<Record<string, number>>(`/api/backup/restore?settings=${restoreSettings}`, restoreFile.data)
      toast(
        `Restored ${r.places} places, ${r.filters} filters, ${r.exclusions} mutes, ${r.overrides} military overrides` +
          (r.places_existing || r.filters_existing ? ` (${r.places_existing + r.filters_existing} already here, kept)` : ''),
      )
      restoreFile = null
      alerting = await get<Alerting>('/api/settings/alerting')
      prediction = await get<PredictionSettings>('/api/settings/prediction')
      exclusions = await get<Exclusion[]>('/api/exclusions')
    } catch (err) {
      toast(errorMessage(err), 'error')
    } finally {
      restoring = false
    }
  }

  async function unmute(x: Exclusion) {
    await del(`/api/exclusions/${x.id}`)
    exclusions = exclusions.filter((e) => e.id !== x.id)
  }

  async function saveHA() {
    busy = true
    try {
      await put('/api/settings/ha', { url: haUrl, token: haToken || null, service: haService })
      haToken = ''
      ha = await get<HAState>('/api/settings/ha')
      toast('Home Assistant settings saved')
    } catch (e) {
      toast(errorMessage(e), 'error')
    } finally {
      busy = false
    }
  }

  async function testHA() {
    busy = true
    testResult = null
    try {
      await saveHA()
      const r = await post<{ ok: boolean; message?: string; error?: string; services?: string[] }>(
        '/api/settings/ha/test',
      )
      testResult = r
      if (r.ok && r.services) {
        services = r.services
        if (!haService && services.length === 1) {
          haService = services[0]
          await saveHA()
        }
      }
    } catch (e) {
      testResult = { ok: false, error: errorMessage(e) }
    } finally {
      busy = false
    }
  }

  async function testNotification() {
    const r = await post<{ ok: boolean; error?: string }>('/api/settings/ha/test-notification')
    toast(r.ok ? 'Sent. Check your phone.' : (r.error ?? 'Failed'), r.ok ? 'info' : 'error')
  }

  async function changePassword(e: SubmitEvent) {
    e.preventDefault()
    try {
      await post('/api/auth/password', { current: pwCurrent, new: pwNew })
      pwCurrent = pwNew = ''
      toast('Password changed')
    } catch (err) {
      toast(errorMessage(err), 'error')
    }
  }

  async function regenerateCodes() {
    if (!confirm('Replace all recovery codes? The old ones stop working.')) return
    const r = await post<{ recovery_codes: string[] }>('/api/auth/recovery-codes')
    newCodes = r.recovery_codes
  }

  async function logoutEverywhere() {
    if (!confirm('Sign out on every device, including this one?')) return
    await post('/api/auth/logout-everywhere')
    loggedOut()
  }
</script>

<div class="page stack">
  <h1>Settings</h1>

  <section class="card stack">
    <h2>Home Assistant</h2>
    <p class="muted small">
      Notifications go to your phone through the Home Assistant companion app. Create a long-lived
      access token in Home Assistant under your profile → Security.
    </p>
    {#if ha}
      <label class="field">
        Home Assistant URL
        <span class="hint">
          From inside Docker: <code>http://host.docker.internal:8123</code> if HA uses host networking,
          or <code>http://homeassistant:8123</code> on a shared Docker network.
        </span>
        <input bind:value={haUrl} disabled={ha.from_env.url} />
      </label>
      <label class="field">
        Long-lived access token
        <input
          type="password"
          bind:value={haToken}
          placeholder={ha.token_set ? '•••••• saved (leave blank to keep)' : ''}
          disabled={ha.from_env.token}
          autocomplete="off"
        />
      </label>
      <div class="row">
        <button onclick={testHA} disabled={busy}>Save &amp; test connection</button>
        {#if testResult}
          <span class={testResult.ok ? 'ok' : 'error'}>
            {testResult.ok ? `✓ ${testResult.message}` : testResult.error}
          </span>
        {/if}
      </div>

      <label class="field">
        Phone (notify service)
        <select bind:value={haService} onchange={saveHA} disabled={ha.from_env.service}>
          <option value={null}>— test the connection to list phones —</option>
          {#each services as s (s)}<option value={s}>{s}</option>{/each}
        </select>
      </label>
      <div><button onclick={testNotification} disabled={!haService}>Send test notification</button></div>
    {/if}
  </section>

  {#if alerting}
    <section class="card stack">
      <h2>Alerting</h2>
      <div class="grid-2">
        <label class="field">
          Time zone <span class="hint">For time-of-day filters and quiet hours</span>
          <input bind:value={alerting.timezone} />
        </label>
        <div class="field-btn">
          {#if alerting.timezone !== deviceTz}
            <button class="small" onclick={() => (alerting!.timezone = deviceTz)}>Use this device's ({deviceTz})</button>
          {/if}
        </div>
        <label class="field">
          An alert ends after the aircraft stops matching for (s)
          <input type="number" min="10" max="3600" bind:value={alerting.episode_end_after_s} />
        </label>
        <label class="field">
          Daily safety limit <span class="hint">Home Assistant allows 500 pushes/day per phone</span>
          <input type="number" min="1" max="490" bind:value={alerting.daily_ceiling} />
        </label>
      </div>
      <p class="muted small">Optional, for all filters (off by default):</p>
      <div class="row">
        <label class="check">
          <input type="checkbox" checked={alerting.repeat_suppress_min !== null}
            onchange={(e) => (alerting!.repeat_suppress_min = (e.currentTarget as HTMLInputElement).checked ? 60 : null)} />
          Don't notify about the same aircraft again within
        </label>
        {#if alerting.repeat_suppress_min !== null}
          <input type="number" class="num" min="1" bind:value={alerting.repeat_suppress_min} /> min
        {/if}
      </div>
      <div class="row">
        <label class="check">
          <input type="checkbox" checked={alerting.quiet_hours !== null}
            onchange={(e) => (alerting!.quiet_hours = (e.currentTarget as HTMLInputElement).checked ? { start: '22:00', end: '07:00' } : null)} />
          Quiet hours (critical filters still notify)
        </label>
        {#if alerting.quiet_hours}
          <input type="time" class="time" bind:value={alerting.quiet_hours.start} /> to
          <input type="time" class="time" bind:value={alerting.quiet_hours.end} />
        {/if}
      </div>
      <div><button class="primary" onclick={saveAlerting}>Save alerting settings</button></div>
    </section>
  {/if}

  {#if prediction}
    <section class="card stack">
      <h2>Prediction</h2>
      <p class="muted small">
        Projects each aircraft's path to say when it will become visible, enter an area or reach an airport.
        Filters use it through the <em>Predicted</em> fields. Places can override the lead time and confidence.
      </p>
      <label class="check"><input type="checkbox" bind:checked={prediction.enabled} /> Predict arrivals</label>
      {#if prediction.enabled}
        <div class="grid-2">
          <label class="field">
            Earliest warning (min) <span class="hint">Predictions further out than this don't count</span>
            <input type="number" min="0.5" max="60" step="0.5" value={prediction.lead_max_s / 60}
              onchange={(e) => (prediction!.lead_max_s = Math.round(+(e.currentTarget as HTMLInputElement).value * 60))} />
          </label>
          <label class="field">
            Minimum confidence (%) <span class="hint">Higher means fewer false alarms but later warnings</span>
            <input type="number" min="5" max="100" step="5" value={Math.round(prediction.min_confidence * 100)}
              onchange={(e) => (prediction!.min_confidence = +(e.currentTarget as HTMLInputElement).value / 100)} />
          </label>
          <label class="field">
            Look ahead (min) <span class="hint">How far the path is projected</span>
            <input type="number" min="1" max="30" step="1" value={prediction.horizon_s / 60}
              onchange={(e) => (prediction!.horizon_s = Math.round(+(e.currentTarget as HTMLInputElement).value * 60))} />
          </label>
        </div>
      {/if}
      <div><button class="primary" onclick={savePrediction}>Save prediction settings</button></div>
    </section>
  {/if}

  <section class="card stack">
    <h2>Muted</h2>
    {#each exclusions as x (x.id)}
      <div class="row between mute">
        <span>
          <strong>{x.kind.replace('_', ' ')}</strong> {x.value}
          <span class="muted small">
            {x.filter_id ? `· filter #${x.filter_id}` : '· all filters'}
            · {x.until ? `until ${fmtTime(x.until)}` : 'forever'} {x.note ? `· ${x.note}` : ''}
          </span>
        </span>
        <button class="small" onclick={() => unmute(x)}>Unmute</button>
      </div>
    {:else}
      <p class="muted small">Nothing muted. Use the mute buttons on an alert or aircraft.</p>
    {/each}
  </section>

  <section class="card stack">
    <h2>Backup</h2>
    <p class="muted small">
      One file with your places, filters, mutes, settings and military overrides. It includes your home
      location but never your password, 2FA or Home Assistant token. Keep it private.
    </p>
    <div class="row">
      <a class="btn" href="/api/backup" download>Download backup</a>
      <label class="btn">
        Restore from file…
        <input type="file" accept="application/json,.json" class="hidden-input" onchange={pickBackup} />
      </label>
    </div>
    {#if restoreFile}
      <div class="restore stack">
        <p>
          <strong>{restoreFile.name}</strong>: {count('places')} places, {count('filters')} filters,
          {count('exclusions')} mutes, {count('military_overrides')} military overrides.
        </p>
        <p class="muted small">
          Restoring adds what's missing. Places and filters that already exist (same name) are kept as they are,
          and this install's home stays home.
        </p>
        <label class="check"><input type="checkbox" bind:checked={restoreSettings} /> Also restore alerting, prediction and classifier settings</label>
        <div class="row">
          <button class="primary" onclick={doRestore} disabled={restoring}>{restoring ? 'Restoring…' : 'Restore'}</button>
          <button onclick={() => (restoreFile = null)}>Cancel</button>
        </div>
      </div>
    {/if}
  </section>

  <section class="card stack">
    <h2>Account</h2>
    <p class="muted small">
      Signed in as <strong>{session.me?.username}</strong>. Recovery codes left:
      {session.me?.recovery_codes_left}.
    </p>
    <form class="stack" onsubmit={changePassword}>
      <div class="grid-2">
        <label class="field">
          Current password
          <input type="password" bind:value={pwCurrent} autocomplete="current-password" required />
        </label>
        <label class="field">
          New password
          <input type="password" bind:value={pwNew} autocomplete="new-password" minlength="12" required />
        </label>
      </div>
      <div><button>Change password</button></div>
    </form>
    <div class="row">
      <button onclick={regenerateCodes}>New recovery codes</button>
      <button class="danger" onclick={logoutEverywhere}>Sign out everywhere</button>
    </div>
    {#if newCodes.length}
      <div class="notice">
        Save these now; they won't be shown again:
        <ul>{#each newCodes as c (c)}<li><code>{c}</code></li>{/each}</ul>
      </div>
    {/if}
  </section>
</div>

<style>
  .hidden-input {
    display: none;
  }
  .restore {
    border-top: 1px solid var(--border);
    padding-top: 8px;
  }
  .num {
    width: 90px;
  }
  .time {
    width: 120px;
  }
  .field-btn {
    display: flex;
    align-items: flex-end;
  }
  .mute {
    border-bottom: 1px solid var(--border);
    padding: 6px 0;
  }
</style>
