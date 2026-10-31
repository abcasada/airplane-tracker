<script lang="ts">
  import type { Snippet } from 'svelte'

  let { title, onclose, children }: { title: string; onclose: () => void; children: Snippet } = $props()
</script>

<svelte:window onkeydown={(e) => e.key === 'Escape' && onclose()} />

<div class="backdrop" role="presentation" onclick={(e) => e.target === e.currentTarget && onclose()}>
  <div class="modal card" role="dialog" aria-modal="true" aria-label={title}>
    <div class="row between head">
      <h2>{title}</h2>
      <button class="icon ghost" onclick={onclose} aria-label="Close">✕</button>
    </div>
    <div class="body">{@render children()}</div>
  </div>
</div>

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    z-index: 900;
    background: rgb(0 0 0 / 45%);
    display: flex;
    align-items: flex-end;
    justify-content: center;
  }
  .modal {
    width: 100%;
    max-width: 640px;
    max-height: 88vh;
    display: flex;
    flex-direction: column;
    border-radius: var(--radius) var(--radius) 0 0;
  }
  .head h2 {
    margin: 0;
  }
  .body {
    overflow: auto;
    margin-top: 10px;
  }
  @media (min-width: 700px) {
    .backdrop {
      align-items: center;
    }
    .modal {
      border-radius: var(--radius);
    }
  }
</style>
