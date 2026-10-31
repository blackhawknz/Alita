'use client';

import { UserRound } from 'lucide-react';
import PersonaEditor from '../persona/PersonaEditor';
import { Section } from '../ui';

export default function PersonaSection() {
  return (
    <Section
      title="Persona"
      icon={<UserRound className="h-4 w-4" />}
      description="Alita's system prompt: who she is and how she should respond. Included in every conversation."
    >
      <PersonaEditor />
    </Section>
  );
}
