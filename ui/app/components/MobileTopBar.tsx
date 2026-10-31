'use client';

import { Menu } from 'lucide-react';

export default function MobileTopBar({ onMenu }: { onMenu: () => void }) {
  return (
    <div className="flex h-12 flex-none items-center gap-2 border-b border-line px-2">
      <button className="icon-btn" onClick={onMenu} aria-label="Open sidebar" title="Open sidebar">
        <Menu className="h-5 w-5" />
      </button>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src="/logo-trns.png" alt="Alita" className="h-6 w-auto" />
    </div>
  );
}
