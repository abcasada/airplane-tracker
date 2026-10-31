<script lang="ts">
  import { get } from '../lib/api'

  let { hex }: { hex: string } = $props()

  interface Photo {
    source: 'plane-alert-db' | 'planespotters'
    src: string
    link: string | null
    credit: string | null
  }

  let photo = $state<Photo | null>(null)

  $effect(() => {
    const h = hex
    photo = null
    get<Photo | null>(`/api/photos/${h}`)
      .then((p) => {
        if (h === hex) photo = p
      })
      .catch(() => {})
  })
</script>

{#if photo}
  <figure>
    {#if photo.link}
      <a href={photo.link} target="_blank" rel="noopener"><img src={photo.src} alt="" referrerpolicy="no-referrer" /></a>
    {:else}
      <img src={photo.src} alt="" referrerpolicy="no-referrer" />
    {/if}
    <figcaption class="small muted">
      {#if photo.source === 'planespotters'}
        Photo © {photo.credit} · <a href={photo.link} target="_blank" rel="noopener">Planespotters.net</a>
      {:else}
        Photo via plane-alert-db
      {/if}
    </figcaption>
  </figure>
{/if}

<style>
  figure {
    margin: 0;
  }
  img {
    width: 100%;
    max-height: 280px;
    object-fit: cover;
    border-radius: var(--radius-sm);
    display: block;
  }
  figcaption {
    margin-top: 4px;
  }
</style>
