import { useEffect, useRef, type RefObject } from "react";

const modalStack: HTMLElement[] = [];
const selector = "button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], summary, [tabindex]:not([tabindex='-1'])";

/** Only the topmost modal owns keyboard focus, including nested dialogs. */
export function useModalFocus<T extends HTMLElement>(ref: RefObject<T | null>, onClose: () => void, active = true) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const modal = ref.current;
    if (!active || !modal) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    modalStack.push(modal);
    const top = () => modalStack[modalStack.length - 1] === modal;
    const elements = () => Array.from(modal.querySelectorAll<HTMLElement>(selector))
      .filter((element) => element.tabIndex >= 0 && element.getClientRects().length > 0 && !element.closest("[inert]"));
    const focusFirst = () => (elements()[0] ?? modal).focus();
    const frame = requestAnimationFrame(() => { if (top()) focusFirst(); });
    const keydown = (event: KeyboardEvent) => {
      if (!top() || event.isComposing) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        closeRef.current();
      } else if (event.key === "Tab") {
        const items = elements();
        const first = items[0];
        const last = items[items.length - 1];
        if (!first) { event.preventDefault(); modal.focus(); }
        else if (event.shiftKey && (document.activeElement === first || document.activeElement === modal)) {
          event.preventDefault(); last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault(); first.focus();
        }
      }
    };
    const focusin = (event: FocusEvent) => {
      if (top() && event.target instanceof Node && !modal.contains(event.target)) focusFirst();
    };
    document.addEventListener("keydown", keydown, true);
    document.addEventListener("focusin", focusin);
    return () => {
      cancelAnimationFrame(frame);
      modalStack.splice(modalStack.indexOf(modal), 1);
      document.removeEventListener("keydown", keydown, true);
      document.removeEventListener("focusin", focusin);
      if (previous?.isConnected) previous.focus();
    };
  }, [active, ref]);
}
