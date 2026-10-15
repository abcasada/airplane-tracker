<script lang="ts">
  import { onMount } from 'svelte'
  import AircraftRow from '../components/AircraftRow.svelte'
  import { del, errorMessage, get, post, put } from '../lib/api'
  import { navigate } from '../lib/router.svelte'
  import { toast } from '../lib/toast.svelte'
  import type { AircraftSummary } from '../lib/types'

  interface ClassifierSettings {
    threshold: number
    weights: Record<string, number>
    dual_use_max_weight: number
    callsign_prefixes: string[]
    registration_patterns: string[]
    owner_keywords: string[]
    government_keywords: string[]
    police_keywords: string[]
  }
  interface ReviewItem extends AircraftSummary {
    reasons: string[]
    signals: string[]
    override: boolean | null
  }

  const WEIGHT_LABELS: Record<string, string> = {
    db_flag: 'Military flag in aircraft database',
    plane_alert_mil: 'plane-alert-db says military',
    mil_address: 'Transponder address in a military block',
    mil_type: 'Military-only aircraft type',
    owner_keyword: 'Owner/operator name (Air Force, Navy…)',
    callsign_prefix: 'Military callsign prefix (RCH, SAM…)',
    registration_pattern: 'Registration looks like a military serial',
    tactical_callsign: 'Tactical-looking callsign (word + number)',
  }

  let settings = $state<ClassifierSettings | null>(null)
  let review = $state<ReviewItem[]>([])
  let overrides = $state<{ key: string; is_military: boolean; note: string }[]>([])
  let lists = $state<Record<string, string>>({})

  async function load() {
    const [s, r, o] = await Promise.all([
      get<ClassifierSettings>('/api/military/settings'),
      get<{ aircraft: ReviewItem[] }>('/api/military/review'),
      get<{ key: string; is_military: boolean; note: string }[]>('/api/military/overrides'),
    ])
    settings = s
    review = r.aircraft
    overrides = o
    lists = {
      callsign_prefixes: s.callsign_prefixes.join('\n'),
      registration_patterns: s.registration_patterns.join('\n'),
      owner_keywords: s.owner_keywords.join('\n'),
      government_keywords: s.government_keywords.join('\n'),
      police_keywords: s.police_keywords.join('\n'),
    }
  }

  onMount(load)

  async function save() {
    if (!settings) return
    const body = { ...$state.snapshot(settings) }
    for (const [k, v] of Object.entries(lists)) {
      ;(body as unknown as Record<string, string[]>)[k] = v
        .split('\n')
        .map((x) => x.trim())
        .filter(Boolean)
    }
    try {
      await put('/api/military/settings', body)
      toast('Classifier updated')
      await load()
    } catch (e) {
      toast(errorMessage(e), 'error')
    }
  }

  async function reset() {
    if (!confirm('Reset weights and lists to the defaults?')) return
    await del('/api/military/settings')
    await load()
  }

  async function setOverride(a: ReviewItem, value: boolean) {
    await post('/api/military/overrides', { key: `hex:${a.hex}`, is_military: value, note: a.callsign ?? '' })
    await load()
  }

  async function removeOverride(key: string) {
    await del(`/api/military/overrides/${encodeURIComponent(key)}`)
    await load()
  }
</script>

<div class="page stack">
  <h1>Military</h1>
  <p class="muted small">
    Each clue adds to a 0–1 military score. Aircraft at or above the threshold count as military in
    filters. You can also test individual clues with the "Military signals" field.
  </p>

  <div class="card">
    <h2>In range now</h2>
    <p class="muted small">Military and borderline aircraft (score ≥ 0.3), with the reasons.</p>
    {#each review as a (a.hex)}
      <div class="review">
        <AircraftRow {a} onclick={() => navigate(`/aircraft/${a.hex}`)} />
        <div class="reasons small">
          <span class="badge" class:mil={a.military}>{a.military_score?.toFixed(2)}</span>
          {a.reasons.join(' · ')}
        </div>
        <div class="row ov">
          {#if a.override === null}
            <button class="small" onclick={() => setOverride(a, !a.military)}>{a.military ? 'Not military' : 'Is military'}</button>
          {:else}
            <span class="small muted">You marked this {a.override ? 'military' : 'not military'}</span>
          {/if}
        </div>
      </div>
    {:else}
      <p class="muted small">No military or borderline aircraft in range right now.</p>
    {/each}
  </div>

  {#if settings}
    <div class="card stack">
      <h2>How the score works</h2>
      <label class="field">
        Threshold: <strong>{settings.threshold.toFixed(2)}</strong>
        <input type="range" min="0.3" max="0.95" step="0.05" bind:value={settings.threshold} />
      </label>
      <div class="weights">
        {#each Object.keys(WEIGHT_LABELS) as k (k)}
          <label class="field">
            {WEIGHT_LABELS[k]}: <strong>{(settings.weights[k] ?? 0).toFixed(2)}</strong>
            <input type="range" min="0" max="0.95" step="0.05" bind:value={settings.weights[k]} />
          </label>
        {/each}
        <label class="field">
          Mixed civil/military type, at most: <strong>{settings.dual_use_max_weight.toFixed(2)}</strong>
          <input type="range" min="0" max="0.9" step="0.05" bind:value={settings.dual_use_max_weight} />
        </label>
      </div>
      <div class="grid-2">
        <label class="field">Callsign prefixes <span class="hint">one per line; a digit must follow</span>
          <textarea rows="6" bind:value={lists.callsign_prefixes}></textarea></label>
        <label class="field">Registration patterns <span class="hint">regular expressions</span>
          <textarea rows="6" bind:value={lists.registration_patterns}></textarea></label>
        <label class="field">Military owner words
          <textarea rows="6" bind:value={lists.owner_keywords}></textarea></label>
        <label class="field">Government owner words
          <textarea rows="6" bind:value={lists.government_keywords}></textarea></label>
        <label class="field">Police owner words
          <textarea rows="6" bind:value={lists.police_keywords}></textarea></label>
      </div>
      <div class="row">
        <button class="primary" onclick={save}>Save</button>
        <button onclick={reset}>Reset to defaults</button>
      </div>
    </div>
  {/if}

  <div class="card">
    <h2>Your overrides</h2>
    {#each overrides as o (o.key)}
      <div class="row between ovrow">
        <span><code>{o.key}</code> → {o.is_military ? 'military' : 'not military'} <span class="muted small">{o.note}</span></span>
        <button class="small" onclick={() => removeOverride(o.key)}>Remove</button>
      </div>
    {:else}
      <p class="muted small">None yet. Use "Is military" / "Not military" on any aircraft.</p>
    {/each}
  </div>
</div>

<style>
  .review {
    border-bottom: 1px solid var(--border);
    padding-bottom: 6px;
  }
  .review :global(.ac) {
    border-bottom: 0;
  }
  .reasons {
    padding: 0 10px;
    color: var(--text-muted);
  }
  .ov {
    padding: 4px 10px;
  }
  .weights {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 10px 20px;
  }
  input[type='range'] {
    padding: 0;
    min-height: 24px;
  }
  .ovrow {
    padding: 6px 0;
    border-bottom: 1px solid var(--border);
  }
</style>
