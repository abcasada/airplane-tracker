<script lang="ts">
  import { errorMessage, get, post, put } from '../lib/api'
  import { refreshSession } from '../lib/session.svelte'

  let step = $state<'account' | 'totp' | 'codes'>('account')
  let setupToken = $state('')
  let username = $state('')
  let password = $state('')
  let password2 = $state('')
  let qrSvg = $state('')
  let secret = $state('')
  let code = $state('')
  let recoveryCodes = $state<string[]>([])
  let saved = $state(false)
  let error = $state('')
  let busy = $state(false)

  async function createAccount(e: SubmitEvent) {
    e.preventDefault()
    error = ''
    if (password !== password2) {
      error = "Passwords don't match"
      return
    }
    busy = true
    try {
      const r = await post<{ qr_svg: string; secret: string }>('/api/setup/account', {
        setup_token: setupToken.trim(),
        username,
        password,
      })
      qrSvg = r.qr_svg
      secret = r.secret
      step = 'totp'
    } catch (err) {
      error = errorMessage(err)
    } finally {
      busy = false
    }
  }

  async function confirm(e: SubmitEvent) {
    e.preventDefault()
    error = ''
    busy = true
    try {
      const r = await post<{ recovery_codes: string[] }>('/api/setup/confirm', {
        setup_token: setupToken.trim(),
        code,
      })
      recoveryCodes = r.recovery_codes
      step = 'codes'
    } catch (err) {
      error = errorMessage(err)
    } finally {
      busy = false
    }
  }

  async function finish() {
    await refreshSession()
    try {
      // Time-of-day filters and quiet hours use this; default to the device's zone.
      const alerting = await get<Record<string, unknown>>('/api/settings/alerting')
      await put('/api/settings/alerting', {
        ...alerting,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      })
    } catch {
      // Not critical: it can be set later in Settings.
    }
    location.hash = '#/'
  }
</script>

<div class="page-narrow">
  <div class="brand">✈ Airplane Tracker — first-run setup</div>

  {#if step === 'account'}
    <form class="card stack" onsubmit={createAccount}>
      <h1>1. Create your account</h1>
      <p class="muted small">
        The setup token is printed in the container log (<code>docker compose logs airplane-tracker</code>)
        or set with <code>SETUP_TOKEN</code>. It stops anyone else from claiming this instance.
      </p>
      <label class="field">
        Setup token
        <input bind:value={setupToken} autocomplete="off" required />
      </label>
      <label class="field">
        Username
        <input bind:value={username} autocomplete="username" required />
      </label>
      <label class="field">
        Password <span class="hint">At least 12 characters. A passphrase works well.</span>
        <input type="password" bind:value={password} autocomplete="new-password" minlength="12" required />
      </label>
      <label class="field">
        Repeat password
        <input type="password" bind:value={password2} autocomplete="new-password" required />
      </label>
      {#if error}<p class="error">{error}</p>{/if}
      <button class="primary" disabled={busy}>Continue</button>
    </form>
  {:else if step === 'totp'}
    <form class="card stack" onsubmit={confirm}>
      <h1>2. Set up two-factor sign-in</h1>
      <p>Scan this with an authenticator app (Google Authenticator, Aegis, 1Password, …).</p>
      <div class="qr">{@html qrSvg}</div>
      <details>
        <summary class="small">Can't scan? Enter the key manually</summary>
        <code class="secret">{secret}</code>
      </details>
      <label class="field">
        6-digit code from the app
        <input bind:value={code} inputmode="numeric" autocomplete="one-time-code" required />
      </label>
      {#if error}<p class="error">{error}</p>{/if}
      <button class="primary" disabled={busy}>Verify</button>
    </form>
  {:else}
    <div class="card stack">
      <h1>3. Save your recovery codes</h1>
      <p>
        Each code signs you in once if you lose your phone. Store them somewhere safe. They won't be
        shown again.
      </p>
      <ul class="codes">
        {#each recoveryCodes as c (c)}<li><code>{c}</code></li>{/each}
      </ul>
      <label class="check"><input type="checkbox" bind:checked={saved} /> I've saved these codes</label>
      <button class="primary" disabled={!saved} onclick={finish}>Finish</button>
    </div>
  {/if}
</div>

<style>
  .brand {
    font-weight: 700;
    margin-bottom: 16px;
    text-align: center;
  }
  .qr {
    display: flex;
    justify-content: center;
    background: #fff;
    padding: 8px;
    border-radius: 8px;
  }
  .secret {
    display: block;
    word-break: break-all;
    margin-top: 6px;
  }
  .codes {
    columns: 2;
    padding-left: 18px;
    margin: 0;
  }
</style>
