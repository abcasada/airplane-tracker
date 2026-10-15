<script lang="ts">
  import { post } from '../lib/api'
  import { route } from '../lib/router.svelte'
  import { loggedOut, session } from '../lib/session.svelte'
  import Home from './Home.svelte'
  import Places from './Places.svelte'
  import Settings from './Settings.svelte'

  const nav = [
    { path: '/', label: 'Home', icon: '⌂' },
    { path: '/places', label: 'Places', icon: '◎' },
    { path: '/settings', label: 'Settings', icon: '⚙' },
  ]

  const section = $derived('/' + (route.parts[0] ?? ''))

  async function logout() {
    await post('/api/auth/logout').catch(() => {})
    loggedOut()
  }
</script>

<header class="topbar">
  <a class="brand" href="#/">✈ Airplane Tracker</a>
  <nav class="desktop-nav">
    {#each nav as item (item.path)}
      <a href={'#' + item.path} class:active={section === item.path}>{item.label}</a>
    {/each}
  </nav>
  <div class="row">
    <span class="muted small user">{session.me?.username}</span>
    <button class="small ghost" onclick={logout}>Sign out</button>
  </div>
</header>

<main>
  {#if section === '/places'}
    <Places />
  {:else if section === '/settings'}
    <Settings />
  {:else}
    <Home />
  {/if}
</main>

<nav class="bottom-nav">
  {#each nav as item (item.path)}
    <a href={'#' + item.path} class:active={section === item.path}>
      <span class="icon">{item.icon}</span>
      <span>{item.label}</span>
    </a>
  {/each}
</nav>

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
    gap: 4px;
    flex: 1;
  }
  .desktop-nav a,
  .bottom-nav a {
    color: var(--text-muted);
    text-decoration: none;
    border-radius: var(--radius-sm);
  }
  .desktop-nav a {
    padding: 6px 10px;
  }
  .desktop-nav a.active,
  .bottom-nav a.active {
    color: var(--accent);
    background: var(--accent-soft);
  }
  .topbar > .row {
    margin-left: auto;
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
  .bottom-nav a {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    font-size: 0.72rem;
    margin: 4px 2px;
  }
  .bottom-nav .icon {
    font-size: 1.15rem;
    line-height: 1;
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
    .bottom-nav {
      display: none;
    }
    main {
      padding-bottom: 24px;
    }
  }
</style>
