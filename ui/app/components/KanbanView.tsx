'use client';

import { useCallback, useEffect, useState, type DragEvent } from 'react';
import { Kanban, Plus, RefreshCw } from 'lucide-react';
import * as api from '@/lib/api';
import { cx } from '@/lib/format';
import type { KanbanCard as KanbanCardType } from '@/lib/types';
import KanbanCardComponent, { CreateCardModal } from './KanbanCard';
import { useToast } from './Toaster';
import { CenteredSpinner, PageHeader } from './ui';

const COLUMNS = [
  { id: 'idea', label: 'Idea', color: 'bg-accent' },
  { id: 'planned', label: 'Planned', color: 'bg-warn' },
  { id: 'executing', label: 'Executing', color: 'bg-teal' },
  { id: 'review', label: 'Review', color: 'bg-primary' },
  { id: 'done', label: 'Done', color: 'bg-muted' },
];

export default function KanbanView() {
  const toast = useToast();
  const [cards, setCards] = useState<KanbanCardType[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [draggedId, setDraggedId] = useState<string | null>(null);
  const [dragOverColumn, setDragOverColumn] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .getKanbanCards()
      .then((c) => {
        setCards(c);
        setError(null);
      })
      .catch((e) => setError(api.errorMessage(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
    // Poll so card status reflects executor activity.
    const interval = setInterval(() => {
      if (document.visibilityState === 'visible') load();
    }, 3000);
    return () => clearInterval(interval);
  }, [load]);

  const handleDragEnd = () => {
    setDraggedId(null);
    setDragOverColumn(null);
  };
  const handleDragOver = (e: DragEvent, columnId: string) => {
    e.preventDefault();
    if (dragOverColumn !== columnId) setDragOverColumn(columnId);
  };
  const handleDragLeave = (e: DragEvent, columnId: string) => {
    // Ignore leave events fired when moving over child elements.
    if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
    if (dragOverColumn === columnId) setDragOverColumn(null);
  };
  const handleDrop = (e: DragEvent, columnId: string) => {
    e.preventDefault();
    const id = draggedId;
    setDraggedId(null);
    setDragOverColumn(null);
    if (!id) return;
    const card = cards.find((c) => c.id === id);
    if (card && card.column !== columnId) {
      setCards((prev) => prev.map((c) => (c.id === id ? { ...c, column: columnId } : c)));
      api.updateKanbanCard(id, { column: columnId }).catch((err) => {
        toast.error(`Move failed: ${api.errorMessage(err)}`);
        load();
      });
    }
  };

  const handleCreate = async (column: string, title: string, description: string, tags: string[]) => {
    try {
      const newCard = await api.createKanbanCard({ title, description, column, tags });
      setCards((prev) => [...prev, newCard]);
      setShowCreate(null);
    } catch (e) {
      toast.error(`Create failed: ${api.errorMessage(e)}`);
    }
  };

  const handleDelete = async (id: string) => {
    const card = cards.find((c) => c.id === id);
    const ok = await toast.confirm({ title: 'Delete card?', message: card?.title, confirmLabel: 'Delete', danger: true });
    if (!ok) return;
    setCards((prev) => prev.filter((c) => c.id !== id));
    try {
      await api.deleteKanbanCard(id);
    } catch (e) {
      toast.error(`Delete failed: ${api.errorMessage(e)}`);
      load();
    }
  };

  const run = (fn: (id: string) => Promise<unknown>, verb: string) => async (id: string) => {
    try {
      await fn(id);
      load();
    } catch (e) {
      toast.error(`${verb} failed: ${api.errorMessage(e)}`);
    }
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Board"
        icon={<Kanban className="h-4 w-4" />}
        subtitle={`${cards.length} card${cards.length !== 1 ? 's' : ''} · drag to move · cards in Executing run autonomously`}
        actions={
          <>
            <button className="btn-primary" onClick={() => setShowCreate('idea')}>
              <Plus className="h-4 w-4" /> <span className="hidden sm:inline">New card</span>
            </button>
            <button className="icon-btn" onClick={load} title="Refresh" aria-label="Refresh">
              <RefreshCw className="h-4 w-4" />
            </button>
          </>
        }
      />

      {loading ? (
        <CenteredSpinner />
      ) : (
        <div className="min-h-0 flex-1 overflow-x-auto overflow-y-hidden">
          {error && <p className="mx-6 mt-4 rounded-lg border border-danger/30 bg-danger/[0.06] px-3 py-2 text-sm text-danger">{error}</p>}
          <div className="flex h-full min-w-max gap-4 p-5 md:p-6">
            {COLUMNS.map((col) => {
              const colCards = cards.filter((c) => c.column === col.id);
              const isDropTarget = dragOverColumn === col.id;
              return (
                <div
                  key={col.id}
                  className="flex w-72 flex-none flex-col"
                  onDragOver={(e) => handleDragOver(e, col.id)}
                  onDragLeave={(e) => handleDragLeave(e, col.id)}
                  onDrop={(e) => handleDrop(e, col.id)}
                >
                  <div
                    className={cx(
                      'mb-2 flex items-center justify-between rounded-lg border px-3 py-2 transition-colors',
                      isDropTarget ? 'border-teal/40 bg-teal/[0.08]' : 'border-line bg-surface',
                    )}
                  >
                    <div className="flex items-center gap-2">
                      <span className={cx('h-2 w-2 rounded-full', col.color)} />
                      <span className="text-[13px] font-semibold text-fg">{col.label}</span>
                      <span className="rounded bg-elevated px-1.5 py-px text-[10.5px] text-muted">{colCards.length}</span>
                    </div>
                    <button className="icon-btn h-6 w-6" onClick={() => setShowCreate(col.id)} title={`Add card to ${col.label}`} aria-label={`Add card to ${col.label}`}>
                      <Plus className="h-3.5 w-3.5" />
                    </button>
                  </div>
                  <div className={cx('min-h-[100px] flex-1 space-y-2 overflow-y-auto rounded-lg px-1 pb-4', isDropTarget && 'bg-teal/[0.03]')}>
                    {colCards.length === 0 ? (
                      <div className="flex h-20 items-center justify-center rounded-lg border border-dashed border-line text-xs text-muted-2">Drop cards here</div>
                    ) : (
                      colCards.map((card) => (
                        <KanbanCardComponent
                          key={card.id}
                          card={card}
                          onDragStart={setDraggedId}
                          onDragEnd={handleDragEnd}
                          onDelete={handleDelete}
                          onRetry={run(api.retryKanbanCard, 'Retry')}
                          onCancel={run(api.cancelKanbanCard, 'Cancel')}
                        />
                      ))
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {showCreate && (
        <CreateCardModal
          column={showCreate}
          onClose={() => setShowCreate(null)}
          onCreate={(title, description, tags) => handleCreate(showCreate, title, description, tags)}
        />
      )}
    </div>
  );
}
