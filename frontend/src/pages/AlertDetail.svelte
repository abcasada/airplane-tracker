<script lang="ts">
  import { onMount } from 'svelte'
  import ExplainTree from '../components/ExplainTree.svelte'
  import QuickActions from '../components/QuickActions.svelte'
  import { errorMessage, get } from '../lib/api'
  import { fmtDuration, fmtTime } from '../lib/format'
  import type { Alert, ExplainNode } from '../lib/types'

  let { id }: { id: string } = $props()

  interface Detail extends Alert {
    facts: Record<string, unknown>
    explanation: ExplainNode | null
    message: string | null
    notifications: { id: number; sent_at: number; ok: number; error: string | null; title: string; message: string }[]
  }

  let a = $state<Detail | null>(null)
  let error = $state('')

  onMount(async () => {
    try {
      a = await get<Detail>(`/api/alerts/${id}`)
    } catch (e) {
      error = errorMessage(e)
    }
  })

  const reasons = $derived((a?.facts?._military_reasons as string[] | undefined) ?? [])
</script>

<div class="page stack">
  <a href="#/alerts" class="small">← Alerts</a>
  {#if error}<p class="error">{error}</p>{/if}
  {#if a}
    <div class="card stack">
      <div class="row between">
        <h1 class="mono">{a.callsign || a.registration || a.hex}</h1>
        {#if a.military}<span class="badge mil">MILITARY</span>{/if}
      </div>
      {#if a.photo_url}<img class="photo" src={a.photo_url} alt="" referrerpolicy="no-referrer" />{/if}
      <p>{a.type_desc || a.type_code || 'Unknown type'} · {a.registration ?? ''} · <a href={`#/aircraft/${a.hex}`} class="mono">{a.hex}</a></p>
      <p class="small">
        Filter <a href={`#/filters/${a.filter_id}`}>{a.filter_name}</a>
        {#if a.mode === 'shadow'}<span class="badge">shadow</span>{/if}
        · started {fmtTime(a.started_at)}
        {#if a.ended_at}· lasted {fmtDuration(a.ended_at - a.started_at)}{:else}· <span class="badge accent">in progress</span>{/if}
        {#if a.closest_nm != null}· closest {a.closest_nm.toFixed(1)} nm{/if}
      </p>
      {#if a.notified}
        <p class="ok small">Notification sent</p>
      {:else}
        <p class="muted small">Not sent{a.suppressed_reason ? `: ${a.suppressed_reason}` : ''}</p>
      {/if}
      {#if a.title}
        <div class="notif">
          <strong>{a.title}</strong>
          <pre>{a.message}</pre>
        </div>
      {/if}
      {#if reasons.length}
        <div>
          <h3>Why military</h3>
          <ul class="small">{#each reasons as r (r)}<li>{r}</li>{/each}</ul>
        </div>
      {/if}
    </div>

    {#if a.explanation}
      <div class="card">
        <h2>Why it matched</h2>
        <p class="muted small">The filter version that fired, evaluated on what the tracker saw at that moment.</p>
        <ExplainTree node={a.explanation} />
      </div>
    {/if}

    <div class="card">
      <h2>Tune</h2>
      <QuickActions
        hex={a.hex}
        registration={a.registration}
        typeCode={a.type_code}
        operator={(a.facts.operator as string | null) ?? null}
        callsign={a.callsign}
        filterId={a.filter_id}
        filterName={a.filter_name}
        military={!!a.military}
      />
    </div>

    {#if a.notifications.length}
      <div class="card">
        <h2>Notifications</h2>
        {#each a.notifications as n (n.id)}
          <p class="small">{fmtTime(n.sent_at)} · {n.ok ? 'delivered to Home Assistant' : `failed: ${n.error}`}</p>
        {/each}
      </div>
    {/if}
  {/if}
</div>

<style>
  h1 {
    margin: 0;
  }
  .photo {
    width: 100%;
    max-height: 260px;
    object-fit: cover;
    border-radius: var(--radius-sm);
  }
  .notif {
    background: var(--surface-2);
    border-radius: var(--radius-sm);
    padding: 10px;
  }
  .notif pre {
    margin: 4px 0 0;
    white-space: pre-wrap;
    font-family: inherit;
    font-size: 0.9rem;
  }
</style>
