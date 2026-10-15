<script lang="ts">
  import { fmtAlt, fmtNum } from '../lib/format'
  import type { AircraftSummary } from '../lib/types'

  let {
    a,
    onclick,
    action,
  }: { a: AircraftSummary; onclick?: () => void; action?: import('svelte').Snippet } = $props()

  const vs = $derived(
    a.vertical_rate_fpm == null ? '' : a.vertical_rate_fpm > 300 ? '↗' : a.vertical_rate_fpm < -300 ? '↘' : '',
  )
</script>

{#snippet content()}
  <div class="main">
    <div class="row">
      <strong class="cs">{a.callsign || a.registration || a.hex}</strong>
      <span class="muted small">{a.type_code ?? ''}</span>
      {#if a.military}<span class="badge mil">MIL</span>{/if}
      {#if a.government}<span class="badge">GOV</span>{/if}
      {#if a.police}<span class="badge">POLICE</span>{/if}
      {#if a.squawk && ['7500', '7600', '7700'].includes(a.squawk)}<span class="badge danger">{a.squawk}</span>{/if}
      {#if a.visible}<span class="badge ok" title="Visible from home now">👁</span>{/if}
    </div>
    <div class="muted small ellipsis">{a.type_desc ?? ''}{a.operator ? ` · ${a.operator}` : ''}</div>
  </div>
  <div class="nums small">
    <div>{a.distance_nm != null ? `${fmtNum(a.distance_nm, 1)} nm ${a.direction ?? ''}` : '—'}</div>
    <div class="muted">{fmtAlt(a.altitude_ft, a.on_ground)} {vs}</div>
  </div>
  {#if action}<div class="act">{@render action()}</div>{/if}
{/snippet}

{#if onclick}
  <button type="button" class="ac clickable" {onclick}>{@render content()}</button>
{:else}
  <div class="ac">{@render content()}</div>
{/if}

<style>
  .ac {
    display: flex;
    gap: 10px;
    align-items: center;
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
  }
  button.ac {
    width: 100%;
    border: 0;
    border-bottom: 1px solid var(--border);
    border-radius: 0;
    background: transparent;
    text-align: left;
    white-space: normal;
    font: inherit;
    color: inherit;
    min-height: 0;
    justify-content: flex-start;
  }
  button.ac:hover {
    background: var(--surface-2);
    filter: none;
  }
  .main {
    flex: 1;
    min-width: 0;
  }
  .cs {
    font-family: var(--mono);
  }
  .ellipsis {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .nums {
    text-align: right;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
</style>
