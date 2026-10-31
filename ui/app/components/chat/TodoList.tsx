'use client';

import { Circle, CircleCheck, CircleDot, CircleX } from 'lucide-react';
import { cx } from '@/lib/format';
import type { Todo } from '@/lib/types';

export function TodoIcon({ status }: { status: string }) {
  switch (status) {
    case 'completed':
      return <CircleCheck className="h-3.5 w-3.5 flex-none text-teal" aria-label="Completed" />;
    case 'in_progress':
      return <CircleDot className="h-3.5 w-3.5 flex-none animate-pulse text-primary" aria-label="In progress" />;
    case 'cancelled':
      return <CircleX className="h-3.5 w-3.5 flex-none text-muted-2" aria-label="Cancelled" />;
    default:
      return <Circle className="h-3.5 w-3.5 flex-none text-muted" aria-label="Pending" />;
  }
}

export default function TodoList({ todos, dense }: { todos: Todo[]; dense?: boolean }) {
  return (
    <ul className={cx(dense ? 'space-y-1' : 'space-y-1.5')}>
      {todos.map((t, i) => (
        <li key={t.id ?? i} className="flex items-start gap-2">
          <span className="mt-[3px]">
            <TodoIcon status={t.status} />
          </span>
          <span
            className={cx(
              'text-[12.5px] leading-snug',
              t.status === 'completed' && 'text-muted line-through decoration-muted-2',
              t.status === 'cancelled' && 'text-muted-2 line-through',
              t.status === 'in_progress' && 'font-medium text-fg',
              t.status === 'pending' && 'text-fg-2',
            )}
          >
            {t.title}
          </span>
        </li>
      ))}
    </ul>
  );
}
