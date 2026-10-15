<script lang="ts">
  import { get } from '../lib/api'

  let {
    values = $bindable([]),
    suggest = null,
    placeholder = 'Type and press Enter',
    upper = false,
  }: {
    values: string[]
    suggest?: string | null
    placeholder?: string
    upper?: boolean
  } = $props()

  let text = $state('')
  let options = $state<{ value: string; label: string }[]>([])
  let open = $state(false)
  let active = $state(-1)
  let timer: ReturnType<typeof setTimeout> | undefined

  function add(v: string) {
    const clean = (upper ? v.toUpperCase() : v).trim()
    if (clean && !values.some((x) => x.toUpperCase() === clean.toUpperCase())) {
      values = [...values, clean]
    }
    text = ''
    options = []
    open = false
    active = -1
  }

  function remove(i: number) {
    values = values.filter((_, j) => j !== i)
  }

  function onInput() {
    if (text.includes(',')) {
      text.split(',').forEach((p, i, arr) => (i < arr.length - 1 ? add(p) : (text = p)))
    }
    if (!suggest) return
    clearTimeout(timer)
    timer = setTimeout(async () => {
      try {
        options = await get<{ value: string; label: string }[]>(
          `/api/fields/suggest?source=${encodeURIComponent(suggest!)}&q=${encodeURIComponent(text)}`,
        )
        open = options.length > 0
        active = -1
      } catch {
        options = []
      }
    }, 200)
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === 'Enter') {
      e.preventDefault()
      if (open && active >= 0) add(options[active].value)
      else if (text.trim()) add(text)
    } else if (e.key === 'Backspace' && !text && values.length) {
      remove(values.length - 1)
    } else if (e.key === 'ArrowDown' && open) {
      e.preventDefault()
      active = Math.min(options.length - 1, active + 1)
    } else if (e.key === 'ArrowUp' && open) {
      e.preventDefault()
      active = Math.max(0, active - 1)
    } else if (e.key === 'Escape') {
      open = false
    }
  }
</script>

<div class="chips">
  {#each values as v, i (v)}
    <span class="chip">
      {v}
      <button type="button" class="x" aria-label={`Remove ${v}`} onclick={() => remove(i)}>×</button>
    </span>
  {/each}
  <div class="entry">
    <input
      bind:value={text}
      oninput={onInput}
      onkeydown={onKey}
      onfocus={() => suggest && onInput()}
      onblur={() => setTimeout(() => {
        if (text.trim()) add(text)
        open = false
      }, 150)}
      {placeholder}
      autocomplete="off"
    />
    {#if open}
      <ul class="menu" role="listbox">
        {#each options as o, i (o.value)}
          <li>
            <button
              type="button"
              class:active={i === active}
              onmousedown={(e) => {
                e.preventDefault()
                add(o.value)
              }}
            >
              <strong>{o.value}</strong>
              {#if o.label}<span class="muted small">{o.label}</span>{/if}
            </button>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
</div>

<style>
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    align-items: center;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 3px;
    background: var(--surface);
    min-height: 38px;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 2px;
    background: var(--accent-soft);
    color: var(--accent);
    border-radius: 999px;
    padding: 1px 4px 1px 10px;
    font-size: 0.88rem;
    font-weight: 500;
  }
  .x {
    min-height: 0;
    padding: 0 6px;
    border: 0;
    background: transparent;
    color: inherit;
    font-size: 1rem;
    line-height: 1.2;
  }
  .entry {
    position: relative;
    flex: 1 1 120px;
    min-width: 100px;
  }
  .entry input {
    border: 0;
    min-height: 30px;
    padding: 4px 6px;
  }
  .entry input:focus-visible {
    outline: none;
  }
  .menu {
    position: absolute;
    z-index: 50;
    top: 100%;
    left: 0;
    min-width: 260px;
    max-height: 260px;
    overflow: auto;
    margin: 4px 0 0;
    padding: 4px;
    list-style: none;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow);
  }
  .menu button {
    width: 100%;
    justify-content: flex-start;
    gap: 8px;
    border: 0;
    background: transparent;
    min-height: 32px;
    text-align: left;
    white-space: normal;
  }
  .menu button.active,
  .menu button:hover {
    background: var(--accent-soft);
  }
</style>
