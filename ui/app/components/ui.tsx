'use client';

import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { LoaderCircle, X } from 'lucide-react';
import { cx } from '@/lib/format';

export function Spinner({ className = 'h-4 w-4' }: { className?: string }) {
  return <LoaderCircle className={cx('animate-spin', className)} aria-hidden />;
}

export function CenteredSpinner({ label }: { label?: string }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 text-muted">
      <Spinner className="h-5 w-5" />
      {label && <p className="text-sm">{label}</p>}
    </div>
  );
}

export function PageHeader({
  title,
  subtitle,
  icon,
  actions,
}: {
  title: string;
  subtitle?: ReactNode;
  icon?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-none items-center justify-between gap-4 border-b border-line px-5 py-3.5 md:px-6">
      <div className="flex min-w-0 items-center gap-3">
        {icon && (
          <div className="flex h-9 w-9 flex-none items-center justify-center rounded-xl border border-line-strong bg-elevated text-primary">
            {icon}
          </div>
        )}
        <div className="min-w-0">
          <h1 className="truncate text-[15px] font-semibold text-fg">{title}</h1>
          {subtitle && <p className="truncate text-xs text-muted">{subtitle}</p>}
        </div>
      </div>
      {actions && <div className="flex flex-none items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Toggle({
  checked,
  onChange,
  disabled,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cx(
        'relative inline-flex h-5 w-9 flex-none items-center rounded-full transition-colors disabled:opacity-40',
        checked ? 'bg-primary-deep' : 'bg-white/10',
      )}
    >
      <span
        className={cx(
          'inline-block h-4 w-4 rounded-full bg-white shadow transition-transform',
          checked ? 'translate-x-[18px]' : 'translate-x-0.5',
        )}
      />
    </button>
  );
}

const modalStack: object[] = [];

export function Modal({
  title,
  onClose,
  children,
  footer,
  wide,
}: {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    // Only the topmost open modal reacts to Escape (modals can be nested).
    const token = {};
    modalStack.push(token);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && modalStack[modalStack.length - 1] === token) closeRef.current();
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      const i = modalStack.indexOf(token);
      if (i >= 0) modalStack.splice(i, 1);
    };
  }, []);

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4 backdrop-blur-[2px] animate-fade-in"
      onMouseDown={onClose}
      role="presentation"
    >
      <div
        role="dialog"
        aria-modal="true"
        className={cx('flex max-h-[90vh] w-full flex-col rounded-2xl border border-line-strong bg-surface shadow-2xl', wide ? 'max-w-2xl' : 'max-w-md')}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-line px-5 py-3.5">
          <h2 className="text-sm font-semibold text-fg">{title}</h2>
          <button className="icon-btn h-7 w-7" onClick={onClose} aria-label="Close" title="Close">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="overflow-y-auto px-5 py-4">{children}</div>
        {footer && <div className="flex justify-end gap-2 border-t border-line px-5 py-3">{footer}</div>}
      </div>
    </div>
  );
}

export interface MenuItemDef {
  label: string;
  icon?: ReactNode;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
}

/** Small dropdown menu anchored to a trigger. */
export function DropdownMenu({
  trigger,
  items,
  align = 'right',
  label,
  className,
  onOpenChange,
}: {
  trigger: ReactNode;
  items: MenuItemDef[];
  align?: 'left' | 'right';
  label: string;
  className?: string;
  onOpenChange?: (open: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<CSSProperties>({});
  const ref = useRef<HTMLDivElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);

  const set = (v: boolean) => {
    if (v && btnRef.current) {
      // Fixed positioning so menus aren't clipped by scrolling containers.
      const r = btnRef.current.getBoundingClientRect();
      const below = window.innerHeight - r.bottom;
      const style: CSSProperties = { position: 'fixed' };
      if (below < items.length * 34 + 16) style.bottom = window.innerHeight - r.top + 4;
      else style.top = r.bottom + 4;
      if (align === 'right') style.right = Math.max(8, window.innerWidth - r.right);
      else style.left = Math.max(8, r.left);
      setPos(style);
    }
    setOpen(v);
    onOpenChange?.(v);
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) set(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') set(false);
    };
    const onScroll = (e: Event) => {
      if (ref.current && e.target instanceof Node && ref.current.contains(e.target)) return;
      set(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    window.addEventListener('resize', onScroll);
    document.addEventListener('scroll', onScroll, true);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('resize', onScroll);
      document.removeEventListener('scroll', onScroll, true);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <div ref={ref} className={cx('relative', className)}>
      <button
        ref={btnRef}
        type="button"
        className="icon-btn"
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={(e) => {
          e.stopPropagation();
          set(!open);
        }}
      >
        {trigger}
      </button>
      {open && (
        <div role="menu" className="menu" style={pos}>
          {items.map((item) => (
            <button
              key={item.label}
              role="menuitem"
              type="button"
              disabled={item.disabled}
              className={cx('menu-item disabled:opacity-40', item.danger && 'text-danger hover:text-danger')}
              onClick={(e) => {
                e.stopPropagation();
                set(false);
                item.onSelect();
              }}
            >
              {item.icon}
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function EmptyState({ icon, title, children }: { icon: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-6 py-16 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-line-strong bg-elevated text-muted">{icon}</div>
      <p className="text-sm font-medium text-fg-2">{title}</p>
      {children && <div className="max-w-sm text-xs leading-relaxed text-muted">{children}</div>}
    </div>
  );
}

export function Section({
  title,
  description,
  icon,
  children,
  actions,
}: {
  title: string;
  description?: ReactNode;
  icon?: ReactNode;
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className="card overflow-hidden">
      <div className="flex items-start justify-between gap-3 border-b border-line px-5 py-3.5">
        <div className="flex items-start gap-2.5">
          {icon && <div className="mt-0.5 text-primary">{icon}</div>}
          <div>
            <h2 className="text-sm font-semibold text-fg">{title}</h2>
            {description && <p className="mt-0.5 text-xs leading-relaxed text-muted">{description}</p>}
          </div>
        </div>
        {actions && <div className="flex flex-none items-center gap-2">{actions}</div>}
      </div>
      <div className="px-5 py-4">{children}</div>
    </section>
  );
}
