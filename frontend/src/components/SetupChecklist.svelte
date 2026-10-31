<script lang="ts">
  import { onMount } from 'svelte'
  import { get, put } from '../lib/api'
  import type { Place } from '../lib/types'

  let hasHome = $state(true)
  let ntfyReady = $state(true)
  let tz = $state('')
  const deviceTz = Intl.DateTimeFormat().resolvedOptions().timeZone

  onMount(async () => {
    const [places, ntfy, alerting] = await Promise.all([
      get<Place[]>('/api/places'),
      get<{ url: string; topic: string }>('/api/settings/ntfy'),
      get<{ timezone: string }>('/api/settings/alerting'),
    ])
    hasHome = places.some((p) => p.is_home)
    ntfyReady = !!ntfy.url && !!ntfy.topic
    tz = alerting.timezone
  })

  async function useDeviceTz() {
    const alerting = await get<Record<string, unknown>>('/api/settings/alerting')
    await put('/api/settings/alerting', { ...alerting, timezone: deviceTz })
    tz = deviceTz
  }
</script>

{#if tz && tz !== deviceTz}
  <div class="notice small row between">
    <span>Time-of-day filters use {tz}, but this device is on {deviceTz}.</span>
    <button class="small" onclick={useDeviceTz}>Use {deviceTz}</button>
  </div>
{/if}
{#if !hasHome || !ntfyReady}
  <div class="card stack setup">
    <h2>Finish setting up</h2>
    <ul>
      <li class:done={hasHome}>
        {hasHome ? '✓' : '○'} Set your home location — <a href="#/places">Places</a>
      </li>
      <li class:done={ntfyReady}>
        {ntfyReady ? '✓' : '○'} Connect your ntfy server and topic — <a href="#/settings">Settings</a>
      </li>
    </ul>
  </div>
{/if}

<style>
  .setup {
    border-color: var(--accent);
  }
  ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  li {
    padding: 3px 0;
  }
  li.done {
    color: var(--ok);
  }
</style>
