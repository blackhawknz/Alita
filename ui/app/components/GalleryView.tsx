'use client';

import { useCallback, useEffect, useState } from 'react';
import { Download, Image as ImageIcon, RefreshCw, Trash2, X } from 'lucide-react';
import * as api from '@/lib/api';
import { formatBytes, parseLocal, shortDateTime } from '@/lib/format';
import type { GalleryImage } from '@/lib/types';
import { useToast } from './Toaster';
import { CenteredSpinner, EmptyState, PageHeader } from './ui';

export default function GalleryView() {
  const toast = useToast();
  const [images, setImages] = useState<GalleryImage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<GalleryImage | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    api
      .getGalleryImages()
      .then((imgs) => {
        setImages(imgs);
        setError(null);
      })
      .catch((e) => setError(api.errorMessage(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(load, [load]);

  useEffect(() => {
    if (!selected) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSelected(null);
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        const i = images.findIndex((x) => x.filename === selected.filename);
        const next = images[i + (e.key === 'ArrowRight' ? 1 : -1)];
        if (next) setSelected(next);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selected, images]);

  const handleDelete = async (img: GalleryImage) => {
    const ok = await toast.confirm({ title: 'Delete image?', message: img.prompt || img.filename, confirmLabel: 'Delete', danger: true });
    if (!ok) return;
    setImages((prev) => prev.filter((x) => x.filename !== img.filename));
    if (selected?.filename === img.filename) setSelected(null);
    try {
      await api.deleteGalleryImage(img.filename);
    } catch (e) {
      toast.error(api.errorMessage(e));
      load();
    }
  };

  const handleDownload = (img: GalleryImage) => {
    const a = document.createElement('a');
    a.href = api.withToken(img.url);
    a.download = img.filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Gallery"
        icon={<ImageIcon className="h-4 w-4" />}
        subtitle={`${images.length} image${images.length !== 1 ? 's' : ''} · generated with EasyDiffusion`}
        actions={
          <button className="icon-btn" onClick={load} title="Refresh" aria-label="Refresh">
            <RefreshCw className="h-4 w-4" />
          </button>
        }
      />

      <div className="min-h-0 flex-1 overflow-y-auto p-5 md:p-6">
        {loading && images.length === 0 ? (
          <CenteredSpinner />
        ) : error ? (
          <EmptyState icon={<ImageIcon className="h-5 w-5" />} title="Could not load images">
            {error}
          </EmptyState>
        ) : images.length === 0 ? (
          <EmptyState icon={<ImageIcon className="h-5 w-5" />} title="No images yet">
            Ask Alita to generate an image to get started.
          </EmptyState>
        ) : (
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-5">
            {images.map((img) => (
              <div
                key={img.filename}
                className="group relative cursor-pointer overflow-hidden rounded-xl border border-line bg-surface transition-colors hover:border-line-strong"
                onClick={() => setSelected(img)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => e.key === 'Enter' && setSelected(img)}
                aria-label={img.prompt || img.filename}
              >
                <div className="aspect-square overflow-hidden bg-black/30">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={api.withToken(img.url)}
                    alt={img.prompt || img.filename}
                    loading="lazy"
                    className="h-full w-full object-cover transition-transform duration-200 group-hover:scale-105"
                  />
                </div>
                <div className="px-2.5 py-2">
                  <p className="truncate text-[11px] text-fg-2">{img.prompt || img.filename}</p>
                  <p className="mt-0.5 text-[10px] text-muted-2">{shortDateTime(parseLocal(img.created_at))}</p>
                </div>
                <div className="absolute right-2 top-2 flex gap-1 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      handleDownload(img);
                    }}
                    className="rounded-lg bg-black/60 p-1.5 text-white backdrop-blur hover:bg-black/80"
                    title="Download"
                    aria-label="Download"
                  >
                    <Download className="h-3.5 w-3.5" />
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      handleDelete(img);
                    }}
                    className="rounded-lg bg-black/60 p-1.5 text-danger backdrop-blur hover:bg-black/80"
                    title="Delete"
                    aria-label="Delete"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {selected && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 p-4 animate-fade-in" onClick={() => setSelected(null)} role="dialog" aria-modal="true">
          <div className="relative flex max-h-full max-w-5xl flex-col items-center" onClick={(e) => e.stopPropagation()}>
            <button onClick={() => setSelected(null)} className="absolute -top-2 right-0 rounded-lg bg-white/10 p-2 text-white hover:bg-white/20" title="Close" aria-label="Close">
              <X className="h-5 w-5" />
            </button>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={api.withToken(selected.url)} alt={selected.prompt || selected.filename} className="mt-10 max-h-[70vh] max-w-full rounded-lg object-contain" />
            <div className="mt-4 w-full max-w-2xl space-y-2">
              <p className="text-sm text-fg">{selected.prompt || '(no prompt)'}</p>
              {selected.negative_prompt && <p className="text-xs text-muted">Negative: {selected.negative_prompt}</p>}
              <div className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted">
                {selected.seed != null && (
                  <span>
                    Seed: <code className="text-teal">{selected.seed}</code>
                  </span>
                )}
                {selected.sampler && <span>Sampler: {selected.sampler}</span>}
                {selected.steps > 0 && <span>Steps: {selected.steps}</span>}
                {selected.cfg_scale > 0 && <span>CFG: {selected.cfg_scale}</span>}
                {selected.width > 0 && selected.height > 0 && (
                  <span>
                    {selected.width}×{selected.height}
                  </span>
                )}
                <span>{formatBytes(selected.size_bytes)}</span>
                <span>{shortDateTime(parseLocal(selected.created_at))}</span>
              </div>
              <div className="flex gap-2 pt-2">
                <button className="btn-secondary" onClick={() => handleDownload(selected)}>
                  <Download className="h-3.5 w-3.5" /> Download
                </button>
                <button className="btn-danger" onClick={() => handleDelete(selected)}>
                  <Trash2 className="h-3.5 w-3.5" /> Delete
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
