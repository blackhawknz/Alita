'use client';

import { memo, useState } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { X } from 'lucide-react';
import { withToken } from '@/lib/api';
import { cx } from '@/lib/format';
import CodeBlock from './CodeBlock';

function MdImage({ src, alt }: { src?: string; alt?: string }) {
  const [open, setOpen] = useState(false);
  const [failed, setFailed] = useState(false);
  if (!src) return null;
  const url = withToken(src);
  if (failed) {
    return (
      <span className="inline-flex items-center gap-1 rounded-md border border-line-strong px-2 py-0.5 text-xs text-muted">
        Image unavailable{alt ? `: ${alt}` : ''}
      </span>
    );
  }
  return (
    <>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={url}
        alt={alt || ''}
        loading="lazy"
        onClick={() => setOpen(true)}
        onError={() => setFailed(true)}
        className="max-h-[480px] cursor-zoom-in"
      />
      {open && (
        <span
          className="fixed inset-0 z-[80] flex cursor-zoom-out items-center justify-center bg-black/85 p-6 animate-fade-in"
          onClick={() => setOpen(false)}
          role="dialog"
          aria-label={alt || 'Image preview'}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={url} alt={alt || ''} className="max-h-full max-w-full rounded-lg border-0 object-contain" />
          <button className="absolute right-4 top-4 rounded-lg bg-white/10 p-2 text-white" aria-label="Close preview" title="Close">
            <X className="h-5 w-5" />
          </button>
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            className="absolute bottom-4 right-4 rounded-lg bg-white/10 px-3 py-1.5 text-xs text-white no-underline"
          >
            Open original
          </a>
        </span>
      )}
    </>
  );
}

const components: Components = {
  code({ className, children, node: _node, ...rest }) {
    const match = /language-([\w+#.-]+)/.exec(className || '');
    const text = String(children ?? '');
    if (!match && !text.includes('\n')) {
      return (
        <code className={className} {...rest}>
          {children}
        </code>
      );
    }
    return <CodeBlock language={match?.[1] ?? ''} code={text.replace(/\n$/, '')} />;
  },
  pre({ children }) {
    return <>{children}</>;
  },
  a({ href, children, node: _node, ...rest }) {
    return (
      <a {...rest} href={href ? withToken(href) : undefined} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    );
  },
  img({ src, alt }) {
    return <MdImage src={typeof src === 'string' ? src : undefined} alt={alt} />;
  },
  table({ children }) {
    return (
      <div className="my-3 overflow-x-auto">
        <table>{children}</table>
      </div>
    );
  },
};

function Markdown({ text, className }: { text: string; className?: string }) {
  return (
    <div className={cx('md', className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

export default memo(Markdown);
