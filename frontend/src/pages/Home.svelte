<script lang="ts">
  import { onMount } from 'svelte'
  import { get } from '../lib/api'
  import type { Place } from '../lib/types'

  let places = $state<Place[]>([])
  let ha = $state<{ url: string; service: string | null; token_set: boolean } | null>(null)
  let loaded = $state(false)

  onMount(async () => {
    ;[places, ha] = await Promise.all([
      get<Place[]>('/api/places'),
      get<{ url: string; service: string | null; token_set: boolean }>('/api/settings/ha'),
    ])
    loaded = true
  })

  const hasHome = $derived(places.some((p) => p.is_home))
  const haReady = $derived(!!ha && !!ha.url && ha.token_set && !!ha.service)
</script>

<div class="page stack">
  <h1>Home</h1>
  {#if loaded && (!hasHome || !haReady)}
    <div class="card stack">
      <h2>Finish setting up</h2>
      <ul class="checklist">
        <li class:done={hasHome}>
          {hasHome ? '✓' : '○'} Set your home location —
          <a href="#/places">Places</a>
        </li>
        <li class:done={haReady}>
          {haReady ? '✓' : '○'} Connect Home Assistant and pick your phone —
          <a href="#/settings">Settings</a>
        </li>
      </ul>
    </div>
  {/if}
  <div class="card">
    <p class="muted">Live aircraft, filters and alerts appear here once the tracker is running.</p>
  </div>
</div>

<style>
  .checklist {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .checklist li {
    padding: 4px 0;
  }
  .checklist li.done {
    color: var(--ok);
  }
</style>
