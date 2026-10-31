'use client';

import { useEffect, useState } from 'react';
import { Code2, Globe, ListChecks, Palette } from 'lucide-react';

const SUGGESTIONS = [
  {
    icon: Globe,
    title: 'Research a topic',
    hint: 'Search the web and cite sources',
    prompt: 'Search the web for the latest developments in open-source AI agents and summarise the key points with sources.',
  },
  {
    icon: Code2,
    title: 'Write and run code',
    hint: 'Analyse data in the sandbox',
    prompt: 'Write a Python script that analyses a CSV of expenses and charts spending by category, then run it on a small sample dataset.',
  },
  {
    icon: Palette,
    title: 'Generate an image',
    hint: 'With EasyDiffusion',
    prompt: 'Generate an image of a cozy cyberpunk reading nook at night, soft neon light, rain on the window.',
  },
  {
    icon: ListChecks,
    title: 'Plan a project',
    hint: 'Break it down and track progress',
    prompt: 'Help me plan a weekend project to build a personal website. Break it into steps and track them as a plan.',
  },
];

function greeting(): string {
  const h = new Date().getHours();
  if (h < 5) return 'Working late?';
  if (h < 12) return 'Good morning';
  if (h < 18) return 'Good afternoon';
  return 'Good evening';
}

export default function ChatEmpty({ onPick }: { onPick: (prompt: string) => void }) {
  const [hello, setHello] = useState('Hello');
  useEffect(() => setHello(greeting()), []);

  return (
    <div className="flex min-h-full flex-col items-center justify-center px-4 py-10 animate-fade-in">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src="/logo-trns.png" alt="Alita" className="mb-5 h-14 w-auto opacity-95" />
      <h1 className="text-center text-2xl font-semibold tracking-tight text-fg">{hello}</h1>
      <p className="mt-1.5 text-center text-sm text-muted">How can I help you today?</p>
      <div className="mt-8 grid w-full max-w-2xl grid-cols-1 gap-2.5 sm:grid-cols-2">
        {SUGGESTIONS.map((s) => (
          <button
            key={s.title}
            onClick={() => onPick(s.prompt)}
            className="group flex items-start gap-3 rounded-xl border border-line bg-surface px-4 py-3 text-left transition-colors hover:border-line-strong hover:bg-elevated"
          >
            <span className="mt-0.5 flex h-8 w-8 flex-none items-center justify-center rounded-lg border border-line-strong bg-elevated text-primary transition-colors group-hover:border-primary/40">
              <s.icon className="h-4 w-4" />
            </span>
            <span className="min-w-0">
              <span className="block text-[13.5px] font-medium text-fg">{s.title}</span>
              <span className="block text-xs text-muted">{s.hint}</span>
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
