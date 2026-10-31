import type { Config } from 'tailwindcss';

// Colours are driven by CSS variables in app/globals.css so the palette lives
// in one place. Channels-based variables allow opacity modifiers (bg-primary/10).
const rgb = (v: string) => `rgb(var(${v}) / <alpha-value>)`;

const config: Config = {
  content: ['./app/**/*.{js,ts,jsx,tsx,mdx}', './lib/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: 'var(--bg)',
        surface: 'var(--surface)',
        elevated: 'var(--elevated)',
        overlay: 'var(--overlay)',
        line: 'var(--border)',
        'line-strong': 'var(--border-bright)',
        fg: rgb('--text-rgb'),
        'fg-2': 'var(--text-2)',
        muted: 'var(--muted)',
        'muted-2': 'var(--muted-2)',
        primary: rgb('--primary-rgb'),
        'primary-deep': rgb('--primary-deep-rgb'),
        teal: rgb('--teal-rgb'),
        warn: rgb('--warning-rgb'),
        danger: rgb('--error-rgb'),
        accent: rgb('--accent-rgb'),
        purple: rgb('--purple-rgb'),
        cyan: rgb('--cyan-rgb'),
      },
      fontFamily: {
        sans: ['"Plus Jakarta Sans"', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', '"Fira Code"', 'ui-monospace', 'monospace'],
      },
      keyframes: {
        fadeIn: { '0%': { opacity: '0', transform: 'translateY(4px)' }, '100%': { opacity: '1', transform: 'none' } },
        slideIn: { '0%': { opacity: '0', transform: 'translateX(-12px)' }, '100%': { opacity: '1', transform: 'none' } },
        toastIn: { '0%': { opacity: '0', transform: 'translateY(8px) scale(0.98)' }, '100%': { opacity: '1', transform: 'none' } },
        blink: { '0%, 100%': { opacity: '1' }, '50%': { opacity: '0' } },
        shimmer: { '0%': { backgroundPosition: '-200% 0' }, '100%': { backgroundPosition: '200% 0' } },
      },
      animation: {
        'fade-in': 'fadeIn 0.2s ease-out',
        'slide-in': 'slideIn 0.2s ease-out',
        'toast-in': 'toastIn 0.18s ease-out',
        blink: 'blink 1s step-end infinite',
        shimmer: 'shimmer 2.2s linear infinite',
      },
    },
  },
  plugins: [],
};

export default config;
