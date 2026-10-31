<script lang="ts">
  import { errorMessage, post } from '../lib/api'
  import { navigate } from '../lib/router.svelte'
  import { toast } from '../lib/toast.svelte'
  import { blankFilter, newId } from '../lib/types'

  let {
    hex,
    registration,
    typeCode,
    operator,
    callsign,
    filterId = null,
    filterName = '',
    military = false,
  }: {
    hex: string
    registration: string | null
    typeCode: string | null
    operator: string | null
    callsign: string | null
    filterId?: number | null
    filterName?: string
    military?: boolean
  } = $props()

  let duration = $state('86400')
  let scopeChoice = $state<'filter' | 'all' | null>(null)
  const scope = $derived(scopeChoice ?? (filterId ? 'filter' : 'all'))

  async function mute(kind: string, value: string | null) {
    if (!value) return
    try {
      await post('/api/exclusions', {
        kind,
        value,
        filter_id: scope === 'filter' ? filterId : null,
        until: duration === 'forever' ? null : Date.now() / 1000 + Number(duration),
        note: `from ${callsign || hex}`,
      })
      toast(`Muted ${kind.replace('_', ' ')} ${value}${scope === 'filter' ? ` for "${filterName}"` : ''}`)
    } catch (e) {
      toast(errorMessage(e), 'error')
    }
  }

  async function override(isMilitary: boolean) {
    try {
      await post('/api/military/overrides', { key: `hex:${hex}`, is_military: isMilitary, note: callsign ?? '' })
      toast(isMilitary ? 'Marked military' : 'Marked not military')
    } catch (e) {
      toast(errorMessage(e), 'error')
    }
  }

  async function followAnywhere() {
    const field = registration ? 'registration' : 'hex'
    const value = registration ?? hex
    try {
      const f = await post<{ id: number }>('/api/filters', {
        name: `Follow ${value}`,
        description: 'Notifies when this aircraft shows up, anywhere.',
        enabled: true,
        mode: 'live',
        definition: {
          root: { type: 'group', id: newId('g'), mode: 'all', enabled: true, children: [
            { type: 'cond', id: newId('c'), field, op: 'in', value: [value], place: null, enabled: true },
          ] },
          display_place_id: null,
        },
        note: 'Created from aircraft page',
      })
      toast(`Following ${value} anywhere`)
      navigate(`/filters/${f.id}`)
    } catch (e) {
      toast(errorMessage(e), 'error')
    }
  }

  function newFilterFromAircraft() {
    const f = blankFilter()
    f.name = `Watch ${typeCode ?? registration ?? hex}`
    f.definition.root.children = [
      typeCode
        ? { type: 'cond', id: newId('c'), field: 'type_code', op: 'in', value: [typeCode], place: null, enabled: true }
        : { type: 'cond', id: newId('c'), field: 'hex', op: 'in', value: [hex], place: null, enabled: true },
    ]
    sessionStorage.setItem('filter-template', JSON.stringify(f))
    navigate('/filters/new')
  }
</script>

<div class="stack">
  <div class="row small">
    Mute
    <select value={scope} onchange={(e) => (scopeChoice = (e.currentTarget as HTMLSelectElement).value as 'filter' | 'all')} class="inline">
      {#if filterId}<option value="filter">for “{filterName}”</option>{/if}
      <option value="all">for all filters</option>
    </select>
    for
    <select bind:value={duration} class="inline">
      <option value="3600">1 hour</option>
      <option value="86400">1 day</option>
      <option value="604800">1 week</option>
      <option value="forever">ever</option>
    </select>
  </div>
  <div class="row">
    <button class="small" onclick={() => mute('hex', hex)}>This aircraft</button>
    {#if typeCode}<button class="small" onclick={() => mute('type_code', typeCode)}>Type {typeCode}</button>{/if}
    {#if operator}<button class="small" onclick={() => mute('operator', operator.split(' / ')[0])}>Operator {operator.split(' / ')[0]}</button>{/if}
    {#if callsign}<button class="small" onclick={() => mute('callsign', callsign.replace(/\d+$/, '*'))}>Callsigns {callsign.replace(/\d+$/, '*')}</button>{/if}
  </div>
  <div class="row">
    {#if military}
      <button class="small" onclick={() => override(false)}>Not military</button>
    {:else}
      <button class="small" onclick={() => override(true)}>Is military</button>
    {/if}
    <button class="small primary" onclick={followAnywhere}>Follow anywhere</button>
    <button class="small" onclick={newFilterFromAircraft}>New filter from this aircraft</button>
  </div>
</div>

<style>
  .inline {
    width: auto;
    min-height: 30px;
    padding: 2px 6px;
  }
</style>
