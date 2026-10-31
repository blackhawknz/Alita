import { describe, expect, it } from 'vitest';
import { speechLooksLikeEcho } from '../conversation';

describe('speechLooksLikeEcho', () => {
  it('rejects a verbatim transcript of the current speech', () => {
    expect(speechLooksLikeEcho('The weather is fine today', 'The weather is fine today.')).toBe(true);
  });

  it('rejects a short echoed fragment', () => {
    expect(speechLooksLikeEcho('weather is fine', 'The weather is fine today.')).toBe(true);
  });

  it('accepts a genuinely different utterance', () => {
    expect(speechLooksLikeEcho('stop and show me the calendar', 'The weather is fine today.')).toBe(false);
  });

  it('accepts when most words are new', () => {
    expect(speechLooksLikeEcho('open the calendar please', 'The weather is fine today.')).toBe(false);
  });

  it('handles empty input', () => {
    expect(speechLooksLikeEcho('', 'hello')).toBe(false);
    expect(speechLooksLikeEcho('hello', '')).toBe(false);
  });
});
