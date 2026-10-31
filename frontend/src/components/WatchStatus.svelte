<script lang="ts">
  import { onDestroy, onMount } from 'svelte'
  import { get } from '../lib/api'
  import { fmtAlt } from '../lib/format'
  import type { WatchEntry } from '../lib/types'

  let { filterId, dirty }: { filterId: number; dirty: boolean } = $props()

  let entries = $state<WatchEntry[] | null>(null)
  let timer: ReturnType<typeof setInterval> | undefined

  async function load() {
    try {
      entries = (await get<WatchEntry[]>('/api/watch')).filter((e) => e.filter_ids.includes(filterId))
    } catch {
      entries = null
    }
  }

  onMount(() => {
    load()
    timer = setInterval(load, 30000)
  })
  onDestroy(() => clearInterval(timer))
</script>

{#if entries && entries.length}
  <section class="card stack">
    <h2>Following anywhere</h2>
    <p class="muted small">
      Registrations and hex codes in this filter are looked up wherever they fly, every minute.
      {#if dirty}Save to apply your changes.{/if}
    </p>
    <ul class="watch">
      {#each entries as e (e.kind + e.value)}
        <li>
          <strong class="mono">{e.value}</strong>
          {#if e.live}
            <span class="badge ok">flying</span>
            <span class="small">
              {e.live.near ?? ''} · {fmtAlt(e.live.altitude_ft, e.live.on_ground)}
              {e.live.callsign ? `· ${e.live.callsign}` : ''}
            </span>
            <a class="small" href={`#/aircraft/${e.live.hex}`}>details</a>
          {:else if e.resolved}
            <span class="muted small">not seen right now ({e.hexes.join(', ')})</span>
          {:else}
            <span class="muted small">not in the aircraft database; checking by registration every 5 min</span>
          {/if}
        </li>
      {/each}
    </ul>
  </section>
{/if}

<style>
  .watch {
    list-style: none;
    padding: 0;
    margin: 0;
    display: grid;
    gap: 6px;
  }
  .watch li {
    display: flex;
    gap: 8px;
    align-items: baseline;
    flex-wrap: wrap;
  }
</style>
