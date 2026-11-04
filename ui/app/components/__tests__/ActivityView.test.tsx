import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import type { ActivityResponse } from '@/lib/types';

const getActivity = vi.fn();

vi.mock('@/lib/api', () => ({
  getActivity: () => getActivity(),
  errorMessage: (error: unknown) => String(error),
}));

import ActivityView from '../ActivityView';

const empty: ActivityResponse = {
  running: [],
  recent: [],
  needs_attention: [],
  spend: {
    today: 0,
    month_to_date: 0,
    projected_month: 0,
    executor_today: 0,
    currency: 'USD',
    executor_per_run_cap: 0.5,
    executor_daily_cap: 5,
  },
  counts: { running: 0, failed_24h: 0, pending_approvals: 0 },
};

const populated: ActivityResponse = {
  running: [
    {
      id: 'run-live',
      handle: 'ses_live',
      executor: 'opencode',
      workdir: 'D:/Projects/example',
      task_summary: 'Fix the failing migration test',
      status: 'running',
      started_at: new Date().toISOString(),
      finished_at: null,
      elapsed_ms: 12_000,
      duration_ms: null,
      cost: 0.0123,
      tool_calls: 14,
      last_activity: 'bash: pytest tests -q',
      error: null,
      result_summary: null,
    },
  ],
  recent: [
    {
      id: 'run-done',
      handle: 'ses_done',
      executor: 'opencode',
      workdir: 'D:/Projects/example',
      task_summary: 'Summarise the diff',
      status: 'succeeded',
      started_at: new Date().toISOString(),
      finished_at: new Date().toISOString(),
      elapsed_ms: 45_000,
      duration_ms: 45_000,
      cost: 0.05,
      tool_calls: 3,
      last_activity: 'bash: git diff',
      error: null,
      result_summary: 'Diff summarised',
    },
  ],
  needs_attention: [
    {
      type: 'run',
      id: 'run-bad',
      handle: 'ses_bad',
      executor: 'opencode',
      workdir: 'D:/Projects/example',
      task_summary: 'Broken run',
      status: 'failed',
      started_at: new Date().toISOString(),
      finished_at: new Date().toISOString(),
      elapsed_ms: 800,
      duration_ms: 800,
      cost: 0.001,
      tool_calls: 0,
      last_activity: null,
      error: 'provider exploded',
      result_summary: null,
    },
    {
      type: 'approval',
      approval_id: 'ap-1',
      tool_name: 'execute_code',
      params: {},
      reason: 'needs host execution',
      severity: 'high',
      created_at: 0,
      age_seconds: 5,
    },
  ],
  spend: {
    today: 0.0123,
    month_to_date: 1.23,
    projected_month: 4.56,
    executor_today: 0.0123,
    currency: 'USD',
    executor_per_run_cap: 0.5,
    executor_daily_cap: 5,
  },
  counts: { running: 1, failed_24h: 1, pending_approvals: 1 },
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('ActivityView', () => {
  it('shows a plain empty state when nothing is running', async () => {
    getActivity.mockResolvedValue(empty);
    render(<ActivityView />);

    expect(await screen.findByText('Nothing running')).toBeTruthy();
    expect(screen.getByText('No finished delegated runs yet.')).toBeTruthy();
    expect(screen.queryByText('Needs attention')).toBeNull();
  });

  it('renders running, attention, spend, and recent runs', async () => {
    getActivity.mockResolvedValue(populated);
    render(<ActivityView />);

    expect(await screen.findByText('Fix the failing migration test')).toBeTruthy();
    expect(screen.getByText('Needs attention')).toBeTruthy();
    expect(screen.getByText('Broken run')).toBeTruthy();
    expect(screen.getByText(/execute_code/)).toBeTruthy();
    expect(screen.getByText('Summarise the diff')).toBeTruthy();
    expect(screen.getByText(/14 tool calls/)).toBeTruthy();
    expect(screen.getByText(/bash: pytest tests -q/)).toBeTruthy();
    expect(screen.getByText('Month to date')).toBeTruthy();
    expect(screen.getByText('$1.2300')).toBeTruthy();
  });
});
