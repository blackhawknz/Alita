'use client';

import { useEffect, useState } from 'react';
import { ChevronDown, ListChecks } from 'lucide-react';
import { cx } from '@/lib/format';
import type { Todo } from '@/lib/types';
import TodoList, { TodoIcon } from './TodoList';

export default function PlanWidget({ todos }: { todos: Todo[] }) {
  const done = todos.filter((t) => t.status === 'completed' || t.status === 'cancelled').length;
  const allDone = todos.length > 0 && done === todos.length;
  const [open, setOpen] = useState(!allDone);
  const current = todos.find((t) => t.status === 'in_progress');

  useEffect(() => {
    if (allDone) setOpen(false);
  }, [allDone]);

  if (todos.length === 0) return null;
  const pct = Math.round((done / todos.length) * 100);

  return (
    <div className="mb-2 overflow-hidden rounded-xl border border-line-strong bg-surface/90 backdrop-blur">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-2.5 px-3 py-2 text-left transition-colors hover:bg-white/[0.02]"
      >
        <ListChecks className="h-4 w-4 flex-none text-teal" />
        <span className="flex-none text-[12.5px] font-semibold text-fg-2">Plan</span>
        <span className="flex-none text-[11.5px] text-muted">
          {done}/{todos.length} done
        </span>
        <div className="mx-1 h-1 w-16 flex-none overflow-hidden rounded-full bg-white/[0.07]">
          <div className="h-full rounded-full bg-teal transition-[width] duration-500" style={{ width: `${pct}%` }} />
        </div>
        {!open && current && (
          <span className="flex min-w-0 items-center gap-1.5 text-[12px] text-fg-2">
            <TodoIcon status="in_progress" />
            <span className="truncate">{current.title}</span>
          </span>
        )}
        <span className="flex-1" />
        <ChevronDown className={cx('h-4 w-4 flex-none text-muted transition-transform', open && 'rotate-180')} />
      </button>
      {open && (
        <div className="max-h-48 overflow-y-auto border-t border-line px-3 py-2.5">
          <TodoList todos={todos} dense />
        </div>
      )}
    </div>
  );
}
