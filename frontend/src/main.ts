import { mount } from 'svelte'
import './styles.css'
import App from './App.svelte'

const app = mount(App, { target: document.getElementById('app')! })

if ('serviceWorker' in navigator && import.meta.env.PROD) {
  navigator.serviceWorker.register('/sw.js').catch(() => {})
}

export default app
