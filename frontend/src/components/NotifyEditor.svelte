<script lang="ts">
  import { catalog } from '../lib/catalog.svelte'
  import type { FilterDefinition } from '../lib/types'

  let { definition = $bindable() }: { definition: FilterDefinition } = $props()

  const n = $derived(definition.notify)
  let showTemplates = $state(!!(definition.notify.title_template || definition.notify.message_template))

  function toggleRepeat(on: boolean) {
    n.repeat_suppress_min = on ? 60 : null
  }
  function toggleMax(on: boolean) {
    n.max_per_day = on ? 10 : null
  }
  function toggleQuiet(on: boolean) {
    n.quiet_hours = on ? { start: '22:00', end: '07:00' } : null
  }
</script>

<div class="stack">
  <label class="check"><input type="checkbox" bind:checked={n.enabled} /> Send notifications for this filter</label>

  {#if n.enabled}
    <div class="grid-2">
      <label class="field">
        Priority
        <select bind:value={n.priority}>
          <option value="low">Low (quiet)</option>
          <option value="normal">Normal</option>
          <option value="high">High (pops up)</option>
          <option value="critical">Critical (ignores global quiet hours)</option>
        </select>
      </label>
      <label class="field">
        Android channel
        <span class="hint">Set sound and vibration per channel in Android settings</span>
        <input bind:value={n.channel} maxlength="60" />
      </label>
      <label class="field">
        Distances and directions relative to
        <select bind:value={definition.display_place_id}>
          <option value={null}>— none —</option>
          <option value={0}>Nearest active viewpoint</option>
          {#each catalog.places as p (p.id)}<option value={p.id}>{p.name}</option>{/each}
        </select>
      </label>
    </div>
    <div class="row">
      <label class="check"><input type="checkbox" bind:checked={n.include_photo} /> Include aircraft photo when available</label>
      <label class="check"><input type="checkbox" bind:checked={n.alarm_stream} /> Sound even in Do Not Disturb</label>
      <label class="check" title="For predicted alerts: shows a countdown, and silently updates it (up to 3 times) when the ETA moves by a minute or more">
        <input type="checkbox" bind:checked={n.update_eta} /> Keep the ETA countdown up to date
      </label>
    </div>

    <details bind:open={showTemplates}>
      <summary>Custom text</summary>
      <div class="stack inner">
        <label class="field">
          Title <span class="hint">Leave empty for the default</span>
          <input bind:value={n.title_template} placeholder={'✈ {aircraft} · {distance} {direction} of {place}'} maxlength="300" />
        </label>
        <label class="field">
          Message
          <textarea rows="4" bind:value={n.message_template} maxlength="1000"
            placeholder={'{callsign} · {operator}\n{altitude} · {speed} · {vertical} · {elevation}'}></textarea>
        </label>
        <div class="small muted keys">
          Placeholders:
          {#each Object.entries(catalog.templateKeys) as [k, help] (k)}
            <code title={help}>{`{${k}}`}</code>
          {/each}
        </div>
      </div>
    </details>

    <details>
      <summary>Optional limits <span class="muted small">(all off by default)</span></summary>
      <div class="stack inner">
        <div class="limit">
          <label class="check">
            <input type="checkbox" checked={n.repeat_suppress_min !== null} onchange={(e) => toggleRepeat((e.currentTarget as HTMLInputElement).checked)} />
            Don't notify about the same aircraft again within
          </label>
          {#if n.repeat_suppress_min !== null}
            <input type="number" min="1" bind:value={n.repeat_suppress_min} class="num" /> min
          {/if}
        </div>
        <div class="limit">
          <label class="check">
            <input type="checkbox" checked={n.max_per_day !== null} onchange={(e) => toggleMax((e.currentTarget as HTMLInputElement).checked)} />
            At most
          </label>
          {#if n.max_per_day !== null}
            <input type="number" min="1" bind:value={n.max_per_day} class="num" />
          {/if}
          <span>notifications per day from this filter</span>
        </div>
        <div class="limit">
          <label class="check">
            <input type="checkbox" checked={n.quiet_hours !== null} onchange={(e) => toggleQuiet((e.currentTarget as HTMLInputElement).checked)} />
            Quiet hours
          </label>
          {#if n.quiet_hours}
            <input type="time" bind:value={n.quiet_hours.start} class="time" /> to
            <input type="time" bind:value={n.quiet_hours.end} class="time" />
          {/if}
        </div>
      </div>
    </details>
  {/if}
</div>

<style>
  details summary {
    cursor: pointer;
    font-weight: 600;
    padding: 4px 0;
  }
  .inner {
    padding: 8px 0 0 12px;
  }
  .keys code {
    display: inline-block;
    margin: 2px 4px 0 0;
    padding: 0 4px;
    background: var(--surface-2);
    border-radius: 4px;
  }
  .limit {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }
  .num {
    width: 90px;
  }
  .time {
    width: 120px;
  }
</style>
