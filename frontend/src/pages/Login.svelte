<script lang="ts">
  import { errorMessage, post } from '../lib/api'
  import { refreshSession } from '../lib/session.svelte'

  let username = $state('')
  let password = $state('')
  let code = $state('')
  let error = $state('')
  let busy = $state(false)

  async function submit(e: SubmitEvent) {
    e.preventDefault()
    busy = true
    error = ''
    try {
      await post('/api/auth/login', { username, password, code })
      await refreshSession()
    } catch (err) {
      error = errorMessage(err)
    } finally {
      busy = false
    }
  }
</script>

<div class="page-narrow">
  <div class="brand">✈ Airplane Tracker</div>
  <form class="card stack" onsubmit={submit}>
    <h1>Sign in</h1>
    <label class="field">
      Username
      <input bind:value={username} autocomplete="username" required />
    </label>
    <label class="field">
      Password
      <input type="password" bind:value={password} autocomplete="current-password" required />
    </label>
    <label class="field">
      Authenticator code
      <input
        bind:value={code}
        inputmode="numeric"
        autocomplete="one-time-code"
        placeholder="123456 or a recovery code"
        required
      />
    </label>
    {#if error}<p class="error">{error}</p>{/if}
    <button class="primary" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
  </form>
</div>

<style>
  .brand {
    font-weight: 700;
    font-size: 1.2rem;
    margin-bottom: 16px;
    text-align: center;
  }
</style>
