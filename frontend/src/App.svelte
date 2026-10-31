<script lang="ts">
  import { onMount } from 'svelte'
  import { setUnauthorizedHandler } from './lib/api'
  import { loggedOut, refreshSession, session } from './lib/session.svelte'
  import { toasts } from './lib/toast.svelte'
  import Login from './pages/Login.svelte'
  import Setup from './pages/Setup.svelte'
  import Shell from './pages/Shell.svelte'

  let loadError = $state('')

  onMount(() => {
    setUnauthorizedHandler(() => {
      if (session.status === 'ready') loggedOut()
    })
    refreshSession().catch((e) => (loadError = String(e)))
  })
</script>

{#if loadError}
  <div class="page-narrow"><p class="error">Can't reach the server: {loadError}</p></div>
{:else if session.status === 'loading'}
  <div class="page-narrow muted">Loading…</div>
{:else if session.status === 'setup'}
  <Setup />
{:else if session.status === 'login'}
  <Login />
{:else}
  <Shell />
{/if}

<div class="toast-host" aria-live="polite">
  {#each toasts as t (t.id)}
    <div class="toast" class:error={t.kind === 'error'}>{t.text}</div>
  {/each}
</div>
