import { describe, expect, it } from 'vitest';
import {
  newTurn,
  reduceTurn,
  turnText,
  type AssistantTurn,
  type Block,
} from '../turns';
import type { StreamEvent } from '../types';

let seq = 0;

function event(payload: Record<string, unknown>): StreamEvent {
  seq += 1;
  return { ...payload, seq } as unknown as StreamEvent;
}

function apply(turn: AssistantTurn, ...payloads: Record<string, unknown>[]): AssistantTurn {
  return payloads.reduce((acc, payload) => reduceTurn(acc, event(payload)), turn);
}

function textBlocks(turn: AssistantTurn): string[] {
  return turn.blocks
    .filter((block): block is Extract<Block, { kind: 'text' }> => block.kind === 'text')
    .map((block) => block.text);
}

function reasoningBlocks(turn: AssistantTurn): string[] {
  return turn.blocks
    .filter((block): block is Extract<Block, { kind: 'reasoning' }> => block.kind === 'reasoning')
    .map((block) => block.text);
}

describe('reduceTurn', () => {
  it('accumulates adjacent text deltas', () => {
    const turn = apply(newTurn('t1'), { type: 'text', content: 'Hello' }, { type: 'text', content: ' world' });
    expect(turnText(turn)).toBe('Hello world');
    expect(textBlocks(turn)).toEqual(['Hello world']);
  });

  it('keeps interleaved reasoning and text ordered', () => {
    const turn = apply(
      newTurn('t2'),
      { type: 'text', content: 'First. ' },
      { type: 'reasoning', content: 'Thinking.' },
      { type: 'text', content: 'Second.' },
    );
    expect(turn.blocks.map((block) => block.kind)).toEqual(['text', 'reasoning', 'text']);
    expect(textBlocks(turn)).toEqual(['First. ', 'Second.']);
    expect(reasoningBlocks(turn)).toEqual(['Thinking.']);
  });

  it('pairs tool calls with results', () => {
    const turn = apply(
      newTurn('t3'),
      { type: 'tool_call', id: 'c1', tool: 'file_ops', params: { action: 'read' } },
      { type: 'tool_result', id: 'c1', tool: 'file_ops', success: true, result: { content: 'x' }, duration_ms: 12 },
    );
    const tool = turn.blocks.find((block) => block.kind === 'tool');
    expect(tool?.kind).toBe('tool');
    if (tool?.kind !== 'tool') throw new Error('missing tool');
    expect(tool.tool.status).toBe('success');
    expect(tool.tool.result).toEqual({ content: 'x' });
    expect(tool.tool.durationMs).toBe(12);
  });

  it('moves a tool through approval states', () => {
    const turn = apply(
      newTurn('t4'),
      { type: 'tool_call', id: 'c1', tool: 'terminal', params: { command: 'rm x' } },
      {
        type: 'approval_requested',
        approval_id: 'a1',
        tool_call_id: 'c1',
        tool: 'terminal',
        params: { command: 'rm x' },
        reason: 'destructive',
        severity: 'high',
      },
      { type: 'approval', approval_id: 'a1', tool_call_id: 'c1', approved: true },
    );
    const tool = turn.blocks.find((block) => block.kind === 'tool');
    if (tool?.kind !== 'tool') throw new Error('missing tool');
    expect(tool.tool.status).toBe('running');
    expect(tool.tool.approval?.state).toBe('approved');
  });

  it('records errors and finalises on done', () => {
    const turn = apply(
      newTurn('t5'),
      { type: 'error', message: 'boom' },
      { type: 'done', interrupted: false, error: null, duration_ms: 42 },
    );
    expect(turn.error).toBe('boom');
    expect(turn.status).toBe('error');
    expect(turn.durationMs).toBe(42);
  });

  it('marks an interrupted turn', () => {
    const turn = apply(newTurn('t6'), { type: 'done', interrupted: true, error: null });
    expect(turn.status).toBe('interrupted');
  });

  it('replaces accumulated text and reasoning on a snapshot', () => {
    const turn = apply(
      newTurn('t7'),
      { type: 'text', content: 'earlier ' },
      { type: 'reasoning', content: 'old thought ' },
      { type: 'text', content: 'fragment' },
      {
        type: 'snapshot',
        text: 'earlier fragment plus replayed prefix',
        reasoning: 'old thought plus replayed reasoning',
        step: 3,
      },
      { type: 'text', content: ' and the tail' },
    );
    expect(turnText(turn)).toBe('earlier fragment plus replayed prefix and the tail');
    expect(textBlocks(turn)).toEqual(['earlier fragment plus replayed prefix and the tail']);
    expect(reasoningBlocks(turn)).toEqual(['old thought plus replayed reasoning']);
    expect(turn.step).toBe(3);
  });

  it('ignores duplicate sequence numbers', () => {
    const turn = newTurn('t8');
    const first = { type: 'text', content: 'one', seq: 1 } as StreamEvent;
    const duplicate = { type: 'text', content: 'two', seq: 1 } as StreamEvent;
    const result = reduceTurn(reduceTurn(turn, first), duplicate);
    expect(turnText(result)).toBe('one');
  });
});
