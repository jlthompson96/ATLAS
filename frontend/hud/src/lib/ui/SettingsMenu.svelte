<script lang="ts">
  import { useHud } from '../ctx';
  import {
    localeTemperatureUnit,
    resolveProfile,
    type MotionSetting,
    type ProfileSetting,
    type QualitySetting,
    type TemperatureSetting
  } from '../state/settings.svelte';
  import LayoutEditor from './LayoutEditor.svelte';
  import Modal from './Modal.svelte';

  let { onclose }: { onclose: () => void } = $props();
  const { store, settings } = useHud();

  let editing = $state(false);
  const active = $derived(resolveProfile(settings.profile, store.device?.role, settings.viewportWidth));
</script>

{#if editing}
  <LayoutEditor profile={active} onclose={() => (editing = false)} />
{:else}
  <Modal id="settings" title="Settings" {onclose}>
    <div class="form">
      <label for="set-quality">Orb rendering</label>
      <select id="set-quality" class="input" value={settings.quality} onchange={(e) => settings.set({ quality: e.currentTarget.value as QualitySetting })}>
        <option value="auto">Automatic (full, steps down if slow or hot)</option>
        <option value="full">Full (WebGL2)</option>
        <option value="lite">Lite (Canvas2D)</option>
        <option value="static">Static (no animation)</option>
      </select>

      <label for="set-motion">Motion</label>
      <select id="set-motion" class="input" value={settings.motion} onchange={(e) => settings.set({ motion: e.currentTarget.value as MotionSetting })}>
        <option value="system">Follow system setting{settings.systemReducedMotion ? ' (reduce)' : ''}</option>
        <option value="reduce">Reduce motion</option>
        <option value="full">Allow motion</option>
      </select>

      <label for="set-profile">Layout profile</label>
      <select id="set-profile" class="input" value={settings.profile} onchange={(e) => settings.set({ profile: e.currentTarget.value as ProfileSetting })}>
        <option value="auto">Automatic ({active})</option>
        <option value="kiosk">Kiosk</option>
        <option value="remote">Remote</option>
        <option value="compact">Compact</option>
      </select>

      <label for="set-temperature">Temperature</label>
      <select id="set-temperature" class="input" value={settings.temperature} onchange={(e) => settings.set({ temperature: e.currentTarget.value as TemperatureSetting })}>
        <option value="auto">Automatic ({localeTemperatureUnit(settings.locale) === 'f' ? '°F' : '°C'})</option>
        <option value="f">Fahrenheit (°F, wind in mph)</option>
        <option value="c">Celsius (°C, wind in km/h)</option>
      </select>
    </div>

    <div class="actions">
      <button class="btn" type="button" onclick={() => (editing = true)}>Edit layout ({active})…</button>
      <button class="btn" type="button" onclick={() => store.requestSync()}>Resync</button>
    </div>

    <dl class="kv about">
      <dt>Device</dt>
      <dd>{store.device ? `${store.device.device_id} (${store.device.role})` : '—'}</dd>
      <dt>Approvals</dt>
      <dd>{store.canApprove ? 'allowed' : 'not on this device'}</dd>
      <dt>Server</dt>
      <dd>{store.server ? `${store.server.version} · schema ${store.server.schema_revision}` : '—'}</dd>
    </dl>

    <div class="actions">
      <button class="btn primary" type="button" onclick={onclose}>Done</button>
    </div>
  </Modal>
{/if}

<style>
  .form {
    display: grid;
    gap: 4px;
  }
  label {
    color: var(--muted);
    font-size: 0.78rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    margin-top: 6px;
  }
  .actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 14px;
    flex-wrap: wrap;
  }
  .about {
    margin-top: 14px;
    font-size: 0.8rem;
  }
</style>
