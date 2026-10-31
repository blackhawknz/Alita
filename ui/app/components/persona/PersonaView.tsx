'use client';

import { UserRound } from 'lucide-react';
import { PageHeader } from '../ui';
import PersonaEditor from './PersonaEditor';

const TIPS = [
  'Describe identity and tone ("You are Alita, a warm, witty assistant…").',
  'State expertise and focus areas, and how detailed answers should be.',
  'Add boundaries or habits, e.g. "always ask before deleting files".',
  'Facts about you belong in Memory; Alita learns those automatically.',
];

export default function PersonaView() {
  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Persona"
        icon={<UserRound className="h-4 w-4" />}
        subtitle="Alita's system prompt and your custom instructions. Applies to every chat."
      />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto grid max-w-5xl gap-6 px-5 py-6 md:px-8 lg:grid-cols-[1fr_260px]">
          <div className="card px-5 py-5">
            <PersonaEditor />
          </div>
          <aside className="space-y-3 text-xs leading-relaxed text-muted">
            <h2 className="text-[13px] font-semibold text-fg-2">Tips</h2>
            <ul className="space-y-2">
              {TIPS.map((t) => (
                <li key={t} className="flex gap-2">
                  <span className="mt-1.5 h-1 w-1 flex-none rounded-full bg-primary" />
                  <span>{t}</span>
                </li>
              ))}
            </ul>
            <p>
              Changes take effect from the next message, including in chats that are already open. Use{' '}
              <span className="text-fg-2">Preview full prompt</span> to see exactly what the model receives.
            </p>
          </aside>
        </div>
      </div>
    </div>
  );
}
