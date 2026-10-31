<script lang="ts">
  import { errorMessage, get } from '../lib/api'
  import type { Airspace } from '../lib/types'

  let { near, onuse }: { near: { lat: number; lon: number } | null; onuse: (a: Airspace) => void } = $props()

  let q = $state('')
  let busy = $state(false)
  let error = $state('')
  let results = $state<Airspace[] | null>(null)
  let attribution = $state('')

  async function search(byName: boolean) {
    busy = true
    error = ''
    try {
      const params = byName
        ? `q=${encodeURIComponent(q.trim())}`
        : `lat=${near!.lat}&lon=${near!.lon}&radius_nm=40`
      const r = await get<{ results: Airspace[]; attribution: string }>(`/api/airspace/search?${params}`)
      results = r.results
      attribution = r.attribution
    } catch (e) {
      error = errorMessage(e)
      results = null
    } finally {
      busy = false
    }
  }

  function band(a: Airspace): string {
    return `${a.floor.text} to ${a.ceiling.text}`
  }
</script>

<details class="lookup">
  <summary>Find FAA airspace <span class="muted small">(restricted areas, MOAs, warning areas…)</span></summary>
  <div class="stack inner">
    <div class="row">
      <input
        bind:value={q}
        placeholder="Name, e.g. R-3601 or SMOKY"
        maxlength="40"
        aria-label="Airspace name"
        onkeydown={(e) => e.key === 'Enter' && (e.preventDefault(), q.trim().length >= 2 && search(true))}
      />
      <button type="button" class="small" onclick={() => search(true)} disabled={busy || q.trim().length < 2}>Search</button>
      {#if near}
        <button type="button" class="small" onclick={() => search(false)} disabled={busy}>Near here</button>
      {/if}
    </div>
    {#if busy}<p class="muted small">Searching…</p>{/if}
    {#if error}<p class="error small">{error}</p>{/if}
    {#if results}
      {#each results as a, i (i)}
        <div class="result">
          <div class="row between">
            <strong>{a.name}</strong>
            <button type="button" class="small primary" onclick={() => onuse(a)}>Use</button>
          </div>
          <div class="small">{a.type} · {band(a)}{a.location ? ` · ${a.location}` : ''}</div>
          {#if a.times_of_use}<div class="small muted">Times: {a.times_of_use}</div>{/if}
          {#if a.parts_dropped}<div class="small muted">Has {a.parts_dropped + 1} parts; only the largest is imported.</div>{/if}
        </div>
      {:else}
        <p class="muted small">No airspace found.</p>
      {/each}
      <p class="muted small">Source: {attribution}. Check the chart before relying on it.</p>
    {/if}
  </div>
</details>

<style>
  .lookup summary {
    cursor: pointer;
    font-weight: 600;
  }
  .inner {
    padding: 8px 0 0 12px;
  }
  .row input {
    flex: 1;
    min-width: 0;
  }
  .result {
    border-top: 1px solid var(--border);
    padding-top: 6px;
  }
</style>
