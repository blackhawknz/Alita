import { describe, expect, it } from 'vitest';
import { nextSpeechChunk } from '../voice';

describe('nextSpeechChunk', () => {
  it('waits when there is no boundary and the reply is not final', () => {
    expect(nextSpeechChunk('Hello there', false)).toEqual({ text: '', consumed: 0 });
  });

  it.each(['Hello. Next', 'Wait! Next', 'Really? Next', 'Trailing… next'])(
    'splits on a sentence terminator: %s',
    (sample) => {
      const chunk = nextSpeechChunk(sample, false);
      expect(chunk.text.length).toBeGreaterThan(0);
      expect(chunk.consumed).toBeLessThanOrEqual(sample.length);
      expect(sample.slice(chunk.consumed)).not.toContain(chunk.text);
    },
  );

  it('splits on a paragraph break', () => {
    expect(nextSpeechChunk('Line one\n\nLine two', false)).toEqual({
      text: 'Line one',
      consumed: 10,
    });
  });

  it('falls back to a word boundary for a very long sentence', () => {
    const sample = `${'word '.repeat(60)}tail`;
    const chunk = nextSpeechChunk(sample, false);
    expect(chunk.text.length).toBeGreaterThan(80);
    expect(chunk.text.endsWith('word') || chunk.text.endsWith(' ')).toBe(true);
    expect(chunk.consumed).toBeLessThanOrEqual(sample.length);
  });

  it('returns the remaining text when final', () => {
    expect(nextSpeechChunk('the tail end', true)).toEqual({
      text: 'the tail end',
      consumed: 12,
    });
  });

  it('never returns whitespace-only text', () => {
    expect(nextSpeechChunk('   ', false)).toEqual({ text: '', consumed: 0 });
    expect(nextSpeechChunk('   ', true).text).toBe('');
  });

  it('advances without skipping and terminates', () => {
    let remaining = 'One sentence. Two sentence! Three sentence? And a long tail without punctuation.';
    let guard = 0;
    while (remaining) {
      const chunk = nextSpeechChunk(remaining, false);
      if (!chunk.text) break;
      expect(chunk.consumed).toBeGreaterThan(0);
      expect(chunk.consumed).toBeLessThanOrEqual(remaining.length);
      expect(remaining.startsWith(chunk.text)).toBe(true);
      remaining = remaining.slice(chunk.consumed);
      guard += 1;
      expect(guard).toBeLessThan(20);
    }
    expect(nextSpeechChunk(remaining, true).consumed).toBe(remaining.length);
  });
});
