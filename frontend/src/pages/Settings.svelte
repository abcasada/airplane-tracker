<script lang="ts">
  import { onMount } from 'svelte'
  import { errorMessage, get, post, put } from '../lib/api'
  import { loggedOut, session } from '../lib/session.svelte'
  import { toast } from '../lib/toast.svelte'

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

  let pwCurrent = $state('')
  let pwNew = $state('')
  let newCodes = $state<string[]>([])

  onMount(async () => {
    ha = await get<HAState>('/api/settings/ha')
    haUrl = ha.url || 'http://host.docker.internal:8123'
    haService = ha.service
    if (haService) services = [haService]
  })

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
