'use client';

import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react';
import { CircleAlert, CircleCheck, Info, X } from 'lucide-react';
import { cx } from '@/lib/format';
import { Modal } from './ui';

type ToastKind = 'success' | 'error' | 'info';

interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

interface ConfirmOptions {
  title: string;
  message?: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
}

interface ToastApi {
  success: (msg: string) => void;
  error: (msg: string) => void;
  info: (msg: string) => void;
  confirm: (opts: ConfirmOptions) => Promise<boolean>;
}

const ToastContext = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error('useToast must be used inside <ToastProvider>');
  return ctx;
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const [confirmState, setConfirmState] = useState<(ConfirmOptions & { resolve: (v: boolean) => void }) | null>(null);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => setToasts((t) => t.filter((x) => x.id !== id)), []);

  const push = useCallback(
    (kind: ToastKind, message: string) => {
      const id = nextId.current++;
      setToasts((t) => [...t.filter((x) => x.message !== message), { id, kind, message }].slice(-4));
      setTimeout(() => dismiss(id), kind === 'error' ? 6000 : 3200);
    },
    [dismiss],
  );

  const confirm = useCallback(
    (opts: ConfirmOptions) => new Promise<boolean>((resolve) => setConfirmState({ ...opts, resolve })),
    [],
  );

  const api = useMemo<ToastApi>(
    () => ({
      success: (m) => push('success', m),
      error: (m) => push('error', m),
      info: (m) => push('info', m),
      confirm,
    }),
    [push, confirm],
  );

  const closeConfirm = (v: boolean) => {
    confirmState?.resolve(v);
    setConfirmState(null);
  };

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-[70] flex w-[min(380px,calc(100vw-2rem))] flex-col gap-2" aria-live="polite">
        {toasts.map((t) => {
          const Icon = t.kind === 'success' ? CircleCheck : t.kind === 'error' ? CircleAlert : Info;
          return (
            <div
              key={t.id}
              role={t.kind === 'error' ? 'alert' : 'status'}
              className="pointer-events-auto flex items-start gap-2.5 rounded-xl border border-line-strong bg-overlay px-3.5 py-2.5 text-[13px] text-fg-2 shadow-2xl shadow-black/40 animate-toast-in"
            >
              <Icon
                className={cx(
                  'mt-px h-4 w-4 flex-none',
                  t.kind === 'success' ? 'text-teal' : t.kind === 'error' ? 'text-danger' : 'text-accent',
                )}
              />
              <span className="flex-1 break-words leading-snug">{t.message}</span>
              <button className="text-muted hover:text-fg" onClick={() => dismiss(t.id)} aria-label="Dismiss" title="Dismiss">
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          );
        })}
      </div>
      {confirmState && (
        <Modal
          title={confirmState.title}
          onClose={() => closeConfirm(false)}
          footer={
            <>
              <button className="btn-ghost" onClick={() => closeConfirm(false)}>
                Cancel
              </button>
              <button
                autoFocus
                className={confirmState.danger ? 'btn bg-danger/90 text-white hover:bg-danger' : 'btn-primary'}
                onClick={() => closeConfirm(true)}
              >
                {confirmState.confirmLabel || 'Confirm'}
              </button>
            </>
          }
        >
          <div className="text-sm leading-relaxed text-fg-2">{confirmState.message}</div>
        </Modal>
      )}
    </ToastContext.Provider>
  );
}
