<script lang="ts">
  import { onDestroy } from 'svelte'
  import { del, errorMessage, get, post } from '../lib/api'
  import { fmtTime } from '../lib/format'
  import type { BacktestAircraft, BacktestJob, FilterDefinition } from '../lib/types'

  let {
    definition,
    filterId = null,
    mode = 'live',
    dirty = true,
  }: { definition: FilterDefinition; filterId?: number | null; mode?: 'live' | 'shadow'; dirty?: boolean } = $props()

  let days = $state(1)
  let job = $state<BacktestJob | null>(null)
  let error = $state('')
  let timer: ReturnType<typeof setTimeout> | undefined

  const running = $derived(job?.status === 'running')
  const result = $derived(job?.status === 'done' ? job.result : null)
  const compare = $derived(!!result?.totals.saved && dirty)
  const maxDay = $derived(
    Math.max(1, ...(result?.days ?? []).flatMap((d) => [d.draft.episodes, d.saved?.episodes ?? 0])),
  )

  async function run() {
    error = ''
    clearTimeout(timer)
    try {
      job = await post<BacktestJob>('/api/filters/backtest', {
        definition: $state.snapshot(definition),
        filter_id: filterId,
        mode,
        days,
      })
      poll()
    } catch (e) {
      error = errorMessage(e)
    }
  }

  async function poll() {
    if (!job) return
    try {
      job = await get<BacktestJob>(`/api/filters/backtest/${job.id}`)
    } catch (e) {
      error = errorMessage(e)
      return
    }
    if (job.status === 'running') timer = setTimeout(poll, 1000)
    else if (job.status === 'error') error = job.error ?? 'Backtest failed'
  }

  async function cancel() {
    clearTimeout(timer)
    if (job && running) await del(`/api/filters/backtest/${job.id}`).catch(() => {})
    job = null
  }

  onDestroy(() => {
    clearTimeout(timer)
    if (job && job.status === 'running') del(`/api/filters/backtest/${job.id}`).catch(() => {})
  })

  function n(count: number, word: string): string {
    return `${count} ${word}${count === 1 ? '' : 's'}`
  }

  function label(a: BacktestAircraft): string {
    return [a.callsign || a.registration || a.hex, a.type_code].filter(Boolean).join(' · ')
  }
</script>

<div class="stack">
  <p class="muted small">
    Replays recorded traffic through this filter{filterId && dirty ? ', and compares it with the saved version' : ''}.
    Uses today's places, mutes and settings.
  </p>
  <div class="row">
    <label class="inline">
      Last
      <select bind:value={days} disabled={running}>
        <option value={1}>1 day</option>
        <option value={3}>3 days</option>
        <option value={7}>7 days</option>
        <option value={14}>14 days</option>
      </select>
    </label>
    {#if running}
      <progress max="1" value={job?.progress ?? 0}></progress>
      <button class="small" onclick={cancel}>Cancel</button>
    {:else}
      <button class="primary small" onclick={run}>Run backtest</button>
    {/if}
  </div>
  {#if error}<p class="error small">{error}</p>{/if}

  {#if result}
    {@const d = result.totals.draft}
    {@const s = result.totals.saved}
    <p>
      {compare ? 'This draft' : 'This filter'} would have matched <strong>{d.aircraft}</strong> aircraft
      ({n(d.episodes, 'alert')}, {n(d.notified, 'notification')}){#if compare && s}, vs
        <strong>{s.aircraft}</strong> ({n(s.episodes, 'alert')}, {n(s.notified, 'notification')}) saved{/if}.
      {#if result.predicted}<span class="muted small">Includes predictions.</span>{/if}
    </p>
    {#if result.batches === 0}
      <p class="muted small">No recordings in that period yet. Recording starts when the app runs.</p>
    {/if}

    {#if result.days.length}
      <table class="days small">
        <thead><tr><th>Day</th><th>Alerts</th>{#if compare}<th>Saved</th>{/if}<th>Notified</th></tr></thead>
        <tbody>
          {#each result.days as day (day.date)}
            <tr>
              <td>{day.date}</td>
              <td>
                <span class="bar" style={`width:${(day.draft.episodes / maxDay) * 100}%`}></span>
                {day.draft.episodes}
              </td>
              {#if compare}
                <td>
                  <span class="bar saved" style={`width:${((day.saved?.episodes ?? 0) / maxDay) * 100}%`}></span>
                  {day.saved?.episodes ?? 0}
                </td>
              {/if}
              <td>{day.draft.notified}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}

    {#if compare}
      <details open={result.only_draft.length + result.only_saved.length <= 12}>
        <summary>
          Differences: <span class="badge ok">+{result.only_draft.length}</span>
          <span class="badge danger">−{result.only_saved.length}</span>
          <span class="muted small">{result.both} in both</span>
        </summary>
        <ul class="diff small">
          {#each result.only_draft as a (a.hex)}<li class="plus">+ {label(a)} <span class="muted">{fmtTime(a.first_at)}</span></li>{/each}
          {#each result.only_saved as a (a.hex)}<li class="minus">− {label(a)} <span class="muted">{fmtTime(a.first_at)}</span></li>{/each}
        </ul>
      </details>
    {:else if result.only_draft.length}
      <details>
        <summary>Aircraft ({result.only_draft.length}{result.only_draft.length >= 100 ? '+' : ''})</summary>
        <ul class="diff small">
          {#each result.only_draft as a (a.hex)}<li>{label(a)} <span class="muted">{fmtTime(a.first_at)}</span></li>{/each}
        </ul>
      </details>
    {/if}
  {/if}
</div>

<style>
  .inline {
    display: flex;
    gap: 6px;
    align-items: center;
  }
  progress {
    flex: 1;
    min-width: 80px;
  }
  .days {
    width: 100%;
    border-collapse: collapse;
  }
  .days th,
  .days td {
    text-align: left;
    padding: 2px 6px 2px 0;
    white-space: nowrap;
  }
  .days td {
    position: relative;
  }
  .bar {
    display: inline-block;
    height: 8px;
    max-width: 80px;
    min-width: 2px;
    background: var(--accent);
    border-radius: 2px;
    margin-right: 4px;
    vertical-align: middle;
  }
  .bar.saved {
    background: var(--border);
  }
  .diff {
    list-style: none;
    padding: 0;
    margin: 6px 0 0;
    max-height: 240px;
    overflow: auto;
  }
  .plus {
    color: var(--ok, #2f855a);
  }
  .minus {
    color: var(--danger, #e5484d);
  }
</style>
