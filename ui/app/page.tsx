import { Suspense } from 'react';
import AppShell from './components/AppShell';

function Splash() {
  return (
    <div className="flex h-screen items-center justify-center bg-bg">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src="/logo-trns.png" alt="Alita" className="h-10 w-auto animate-pulse opacity-80" />
    </div>
  );
}

export default function Page() {
  // AppShell reads the URL via useSearchParams, which needs a Suspense boundary
  // for static rendering in Next 14.
  return (
    <Suspense fallback={<Splash />}>
      <AppShell />
    </Suspense>
  );
}
