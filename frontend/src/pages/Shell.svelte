<script lang="ts">
  import { onMount } from 'svelte'
  import { post } from '../lib/api'
  import { connectLive, disconnectLive, live } from '../lib/live.svelte'
  import { route } from '../lib/router.svelte'
  import { loggedOut, session } from '../lib/session.svelte'
  import AircraftDetail from './AircraftDetail.svelte'
  import AlertDetail from './AlertDetail.svelte'
  import Alerts from './Alerts.svelte'
  import FilterEditor from './FilterEditor.svelte'
  import Filters from './Filters.svelte'
  import Live from './Live.svelte'
  import Military from './Military.svelte'
  import Places from './Places.svelte'
  import Settings from './Settings.svelte'
  import System from './System.svelte'

  const primary = [
    { path: '/filters', label: 'Filters', icon: '⧩' },
    { path: '/alerts', label: 'Alerts', icon: '🔔' },
    { path: '/live', label: 'Live', icon: '✈' },
    { path: '/places', label: 'Places', icon: '◎' },
  ]
  const secondary = [
    { path: '/military', label: 'Military' },
    { path: '/system', label: 'System' },
    { path: '/settings', label: 'Settings' },
  ]

  let moreOpen = $state(false)
  const section = $derived('/' + (route.parts[0] ?? 'filters'))
  const sub = $derived(route.parts[1] ?? '')

  onMount(() => {
    connectLive()
    return disconnectLive
  })

  $effect(() => {
    route.path
    moreOpen = false
  })

  async function logout() {
    await post('/api/auth/logout').catch(() => {})
    disconnectLive()
    loggedOut()
  }
</script>

<header class="topbar">
  <a class="brand" href="#/filters">✈ Airplane Tracker</a>
  <nav class="desktop-nav">
    {#each [...primary, ...secondary] as item (item.path)}
      <a href={'#' + item.path} class:active={section === item.path}>{item.label}</a>
    {/each}
  </nav>
  <div class="row right">
    <span class="dot" class:on={live.connected} title={live.connected ? 'Live updates connected' : 'Reconnecting…'}></span>
    <span class="muted small user">{session.me?.username}</span>
    <button class="small ghost" onclick={logout}>Sign out</button>
  </div>
</header>

<main>
  {#key route.path}
    {#if section === '/filters' && sub}
      <FilterEditor id={sub} />
    {:else if section === '/alerts' && sub}
      <AlertDetail id={sub} />
    {:else if section === '/alerts'}
      <Alerts />
    {:else if section === '/aircraft' && sub}
      <AircraftDetail hex={sub} />
    {:else if section === '/live'}
      <Live />
    {:else if section === '/places'}
      <Places />
    {:else if section === '/military'}
      <Military />
    {:else if section === '/system'}
      <System />
    {:else if section === '/settings'}
      <Settings />
    {:else}
      <Filters />
    {/if}
  {/key}
</main>

<nav class="bottom-nav">
  {#each primary as item (item.path)}
    <a href={'#' + item.path} class:active={section === item.path}>
      <span class="icon">{item.icon}</span>
      <span>{item.label}</span>
    </a>
  {/each}
  <button class="more" class:active={secondary.some((s) => s.path === section)} onclick={() => (moreOpen = !moreOpen)}>
    <span class="icon">⋯</span>
    <span>More</span>
  </button>
</nav>

{#if moreOpen}
  <div class="more-menu card">
    {#each secondary as item (item.path)}
      <a href={'#' + item.path}>{item.label}</a>
    {/each}
  </div>
{/if}

<style>
  .topbar {
    position: sticky;
    top: 0;
    z-index: 500;
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 0 16px;
    height: var(--nav-h);
    background: var(--surface);
    border-bottom: 1px solid var(--border);
  }
  .brand {
    font-weight: 700;
    color: var(--text);
    text-decoration: none;
    white-space: nowrap;
  }
  .desktop-nav {
    display: none;
    gap: 2px;
    flex: 1;
  }
  .desktop-nav a,
  .bottom-nav a,
  .bottom-nav button {
    color: var(--text-muted);
    text-decoration: none;
    border-radius: var(--radius-sm);
  }
  .desktop-nav a {
    padding: 6px 10px;
  }
  .desktop-nav a.active,
  .bottom-nav .active {
    color: var(--accent);
    background: var(--accent-soft);
  }
  .right {
    margin-left: auto;
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text-muted);
  }
  .dot.on {
    background: var(--ok);
  }
  main {
    padding-bottom: calc(var(--nav-h) + env(safe-area-inset-bottom) + 16px);
  }
  .bottom-nav {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    z-index: 500;
    display: flex;
    justify-content: space-around;
    height: calc(var(--nav-h) + env(safe-area-inset-bottom));
    padding-bottom: env(safe-area-inset-bottom);
    background: var(--surface);
    border-top: 1px solid var(--border);
  }
  .bottom-nav a,
  .bottom-nav button {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    font-size: 0.72rem;
    margin: 4px 2px;
    border: 0;
    background: transparent;
    min-height: 0;
    padding: 0;
    gap: 0;
  }
  .bottom-nav .icon {
    font-size: 1.15rem;
    line-height: 1.2;
  }
  .more-menu {
    position: fixed;
    right: 8px;
    bottom: calc(var(--nav-h) + env(safe-area-inset-bottom) + 8px);
    z-index: 600;
    display: flex;
    flex-direction: column;
    min-width: 180px;
    padding: 6px;
  }
  .more-menu a {
    padding: 10px 12px;
    color: var(--text);
    text-decoration: none;
    border-radius: var(--radius-sm);
  }
  .more-menu a:hover {
    background: var(--surface-2);
  }
  @media (max-width: 899px) {
    .user {
      display: none;
    }
  }
  @media (min-width: 900px) {
    .desktop-nav {
      display: flex;
    }
    .bottom-nav,
    .more-menu {
      display: none;
    }
    main {
      padding-bottom: 24px;
    }
  }
</style>
