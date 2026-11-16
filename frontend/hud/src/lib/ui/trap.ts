const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusables(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => !el.hidden && el.getAttribute('aria-hidden') !== 'true');
}

/**
 * Svelte action for modal dialogs: moves focus in, keeps Tab inside, closes on Escape and gives focus back
 * to whatever had it before. Keyboard-only operation (A11Y-3) depends on this.
 */
export function trapFocus(node: HTMLElement, onEscape?: () => void) {
  const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  let escape = onEscape;
  const initial = node.querySelector<HTMLElement>('[data-autofocus]') ?? focusables(node)[0] ?? node;
  queueMicrotask(() => initial.focus());

  function onKey(e: KeyboardEvent): void {
    if (e.key === 'Escape') {
      e.stopPropagation();
      escape?.();
      return;
    }
    if (e.key !== 'Tab') return;
    const items = focusables(node);
    if (items.length === 0) {
      e.preventDefault();
      node.focus();
      return;
    }
    const first = items[0] as HTMLElement;
    const last = items[items.length - 1] as HTMLElement;
    const active = document.activeElement;
    if (e.shiftKey && (active === first || !node.contains(active))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (active === last || !node.contains(active))) {
      e.preventDefault();
      first.focus();
    }
  }

  node.addEventListener('keydown', onKey);
  return {
    update(next?: () => void) {
      escape = next;
    },
    destroy() {
      node.removeEventListener('keydown', onKey);
      if (previous?.isConnected) previous.focus();
    }
  };
}
