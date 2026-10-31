'use client';

import { useEffect, useState } from 'react';
import { CircleCheck, CircleX, ExternalLink, Mail, Palette, Plug, Save } from 'lucide-react';
import * as api from '@/lib/api';
import type { GmailStatus, Settings } from '@/lib/types';
import { useToast } from '../Toaster';
import { Section, Spinner } from '../ui';

export default function IntegrationsSection({ settings, onSaved }: { settings: Settings; onSaved: (s: Settings) => void }) {
  const toast = useToast();
  const [edUrl, setEdUrl] = useState(settings.easy_diffusion_url || '');
  const [saving, setSaving] = useState(false);
  const [gmail, setGmail] = useState<GmailStatus | null>(null);
  const [gmailError, setGmailError] = useState<string | null>(null);

  useEffect(() => setEdUrl(settings.easy_diffusion_url || ''), [settings.easy_diffusion_url]);
  useEffect(() => {
    api
      .getGmailStatus()
      .then(setGmail)
      .catch((e) => setGmailError(api.errorMessage(e)));
  }, []);

  const saveEd = async () => {
    setSaving(true);
    try {
      const res = await api.updateSettings({ easy_diffusion_url: edUrl.trim() });
      onSaved(res);
      toast.success('EasyDiffusion URL saved');
    } catch (e) {
      toast.error(api.errorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-5">
      <Section title="Integrations" icon={<Plug className="h-4 w-4" />} description="External services Alita can use.">
        <div className="space-y-6">
          <div>
            <div className="mb-2 flex items-center gap-2">
              <Palette className="h-4 w-4 text-purple" />
              <h3 className="text-[13px] font-semibold text-fg">EasyDiffusion</h3>
            </div>
            <p className="mb-2 text-xs text-muted">Local Stable Diffusion server used for image generation.</p>
            <div className="flex max-w-xl gap-2">
              <input
                value={edUrl}
                onChange={(e) => setEdUrl(e.target.value)}
                placeholder="http://localhost:9000"
                className="input font-mono"
                aria-label="EasyDiffusion URL"
              />
              <button className="btn-primary" onClick={saveEd} disabled={saving || edUrl.trim() === (settings.easy_diffusion_url || '')}>
                {saving ? <Spinner className="h-3.5 w-3.5" /> : <Save className="h-3.5 w-3.5" />} Save
              </button>
            </div>
          </div>

          <div className="border-t border-line pt-5">
            <div className="mb-2 flex items-center gap-2">
              <Mail className="h-4 w-4 text-danger" />
              <h3 className="text-[13px] font-semibold text-fg">Gmail</h3>
            </div>
            {gmailError && <p className="text-xs text-danger">{gmailError}</p>}
            {!gmail && !gmailError && <Spinner className="h-4 w-4 text-muted" />}
            {gmail && (
              <div className="space-y-2.5">
                <p className="flex items-center gap-1.5 text-[13px]">
                  {gmail.authorized ? (
                    <>
                      <CircleCheck className="h-4 w-4 text-teal" /> <span className="text-fg-2">Authorized. Alita can read your mail.</span>
                    </>
                  ) : (
                    <>
                      <CircleX className="h-4 w-4 text-muted" /> <span className="text-fg-2">Not authorized</span>
                    </>
                  )}
                </p>
                {!gmail.has_client_secret ? (
                  <p className="text-xs leading-relaxed text-muted">
                    Save your Google OAuth client credentials JSON to <span className="font-mono text-fg-2">~/.alita/gmail_client_secret.json</span> (or set{' '}
                    <span className="font-mono text-fg-2">GMAIL_CLIENT_SECRET_PATH</span>), then authorize here.
                  </p>
                ) : (
                  <a href={api.withToken('/api/gmail/auth')} target="_blank" rel="noopener noreferrer" className="btn-secondary inline-flex">
                    <ExternalLink className="h-3.5 w-3.5" /> {gmail.authorized ? 'Re-authorize' : 'Authorize Gmail'}
                  </a>
                )}
              </div>
            )}
          </div>

          <p className="border-t border-line pt-4 text-[11px] leading-relaxed text-muted-2">
            Voice providers and ElevenLabs credentials are on the <span className="text-fg-2">Voice</span> tab.
          </p>
        </div>
      </Section>
    </div>
  );
}
