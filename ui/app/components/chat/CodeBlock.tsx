'use client';

import { memo, useState, type CSSProperties } from 'react';
import { Check, Copy, WrapText } from 'lucide-react';
import SyntaxHighlighter from 'react-syntax-highlighter/dist/esm/prism-light';
import oneDark from 'react-syntax-highlighter/dist/esm/styles/prism/one-dark';
import bash from 'react-syntax-highlighter/dist/esm/languages/prism/bash';
import cpp from 'react-syntax-highlighter/dist/esm/languages/prism/cpp';
import csharp from 'react-syntax-highlighter/dist/esm/languages/prism/csharp';
import css from 'react-syntax-highlighter/dist/esm/languages/prism/css';
import diff from 'react-syntax-highlighter/dist/esm/languages/prism/diff';
import docker from 'react-syntax-highlighter/dist/esm/languages/prism/docker';
import go from 'react-syntax-highlighter/dist/esm/languages/prism/go';
import ini from 'react-syntax-highlighter/dist/esm/languages/prism/ini';
import java from 'react-syntax-highlighter/dist/esm/languages/prism/java';
import javascript from 'react-syntax-highlighter/dist/esm/languages/prism/javascript';
import json from 'react-syntax-highlighter/dist/esm/languages/prism/json';
import jsx from 'react-syntax-highlighter/dist/esm/languages/prism/jsx';
import markdown from 'react-syntax-highlighter/dist/esm/languages/prism/markdown';
import markup from 'react-syntax-highlighter/dist/esm/languages/prism/markup';
import powershell from 'react-syntax-highlighter/dist/esm/languages/prism/powershell';
import python from 'react-syntax-highlighter/dist/esm/languages/prism/python';
import rust from 'react-syntax-highlighter/dist/esm/languages/prism/rust';
import sql from 'react-syntax-highlighter/dist/esm/languages/prism/sql';
import toml from 'react-syntax-highlighter/dist/esm/languages/prism/toml';
import tsx from 'react-syntax-highlighter/dist/esm/languages/prism/tsx';
import typescript from 'react-syntax-highlighter/dist/esm/languages/prism/typescript';
import yaml from 'react-syntax-highlighter/dist/esm/languages/prism/yaml';
import { copyText, cx } from '@/lib/format';

const LANGS: Record<string, unknown> = {
  bash, cpp, csharp, css, diff, docker, go, ini, java, javascript, json, jsx, markdown, markup,
  powershell, python, rust, sql, toml, tsx, typescript, yaml,
};
Object.entries(LANGS).forEach(([name, def]) => SyntaxHighlighter.registerLanguage(name, def));

const ALIASES: Record<string, string> = {
  js: 'javascript', mjs: 'javascript', cjs: 'javascript', node: 'javascript',
  ts: 'typescript', py: 'python', python3: 'python',
  sh: 'bash', shell: 'bash', zsh: 'bash', console: 'bash', shellsession: 'bash', terminal: 'bash',
  ps: 'powershell', ps1: 'powershell', pwsh: 'powershell',
  yml: 'yaml', html: 'markup', xml: 'markup', svg: 'markup', vue: 'markup',
  md: 'markdown', rs: 'rust', golang: 'go', c: 'cpp', h: 'cpp', hpp: 'cpp', 'c++': 'cpp',
  cs: 'csharp', dockerfile: 'docker', cfg: 'ini', env: 'ini', patch: 'diff', jsonc: 'json', json5: 'json',
};

const theme: Record<string, CSSProperties> = {
  ...oneDark,
  'pre[class*="language-"]': {
    ...(oneDark['pre[class*="language-"]'] || {}),
    background: 'transparent',
    margin: 0,
    padding: '0.9rem 1rem',
    fontSize: '0.8125rem',
    lineHeight: 1.65,
  },
  'code[class*="language-"]': {
    ...(oneDark['code[class*="language-"]'] || {}),
    background: 'transparent',
    fontFamily: "'JetBrains Mono', ui-monospace, monospace",
    fontSize: '0.8125rem',
  },
};

function CodeBlock({ language, code }: { language: string; code: string }) {
  const [copied, setCopied] = useState(false);
  const [wrap, setWrap] = useState(false);
  const raw = (language || '').toLowerCase();
  const lang = ALIASES[raw] || raw;
  const supported = lang in LANGS;

  const copy = async () => {
    if (await copyText(code)) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    }
  };

  return (
    <div className="group/code my-3 overflow-hidden rounded-xl border border-line-strong bg-[#0b0e16]">
      <div className="flex items-center justify-between border-b border-line bg-white/[0.02] px-3 py-1.5">
        <span className="font-mono text-[11px] lowercase text-muted">{raw || 'text'}</span>
        <div className="flex items-center gap-0.5">
          <button
            onClick={() => setWrap((w) => !w)}
            className={cx('rounded-md p-1 text-muted transition-colors hover:bg-white/5 hover:text-fg-2', wrap && 'text-primary')}
            title={wrap ? 'Disable line wrap' : 'Wrap lines'}
            aria-label={wrap ? 'Disable line wrap' : 'Wrap lines'}
          >
            <WrapText className="h-3.5 w-3.5" />
          </button>
          <button
            onClick={copy}
            className="flex items-center gap-1 rounded-md px-1.5 py-1 text-[11px] text-muted transition-colors hover:bg-white/5 hover:text-fg-2"
            title="Copy code"
            aria-label="Copy code"
          >
            {copied ? <Check className="h-3.5 w-3.5 text-teal" /> : <Copy className="h-3.5 w-3.5" />}
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
      </div>
      <div className="max-h-[560px] overflow-auto">
        {supported ? (
          <SyntaxHighlighter
            language={lang}
            style={theme}
            wrapLongLines={wrap}
            PreTag="pre"
            codeTagProps={{ style: { whiteSpace: wrap ? 'pre-wrap' : 'pre' } }}
          >
            {code}
          </SyntaxHighlighter>
        ) : (
          <pre
            className={cx('px-4 py-3.5 font-mono text-[0.8125rem] leading-[1.65] text-[#abb2bf]', wrap ? 'whitespace-pre-wrap break-words' : 'whitespace-pre')}
          >
            <code>{code}</code>
          </pre>
        )}
      </div>
    </div>
  );
}

export default memo(CodeBlock);
