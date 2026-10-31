import { describe, expect, it } from 'vitest';
import { speechText } from '../voice';

describe('speechText', () => {
  it('removes fenced code blocks before TTS', () => {
    expect(speechText('Before.\n```ts\nconst x = 1;\n```\nAfter.')).toBe('Before.\n \nAfter.');
  });

  it('removes an unclosed fence to the end of the streamed text', () => {
    expect(speechText('Read this. ```python\nprint(1)')).toBe('Read this.  ');
  });

  it('keeps inline code and ordinary punctuation', () => {
    expect(speechText('Use `npm test` now.')).toBe('Use `npm test` now.');
  });
});
