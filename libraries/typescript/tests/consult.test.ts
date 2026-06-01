/**
 * Unit tests for the built-in ``consult`` escalation tool (parity with the
 * Python ``test_consult.py``).
 *
 * No network: SSRF rejection uses the real validator; handler HTTP behaviour
 * is covered in ``consult.integration.test.ts``.
 */

import { describe, expect, it } from 'vitest';
import { buildConsultTool } from '../src/consult';
import type { ConsultConfig } from '../src/types';

describe('[unit] buildConsultTool', () => {
  it('rejects a non-http scheme', () => {
    expect(() => buildConsultTool({ url: 'ftp://orchestrator.example.com' } as ConsultConfig)).toThrow();
  });

  it('rejects an SSRF target (loopback)', () => {
    expect(() => buildConsultTool({ url: 'http://127.0.0.1:9/consult' } as ConsultConfig)).toThrow();
  });

  it('builds a tool with the default name and a request param', () => {
    const tool = buildConsultTool({ url: 'https://orchestrator.example.com/consult' });
    expect(tool.name).toBe('consult_agent');
    expect(typeof tool.handler).toBe('function');
    expect((tool.parameters as { required: string[] }).required).toEqual(['request']);
  });

  it('honours a custom tool name', () => {
    const tool = buildConsultTool({ url: 'https://orchestrator.example.com', toolName: 'ask_brain' });
    expect(tool.name).toBe('ask_brain');
  });
});
