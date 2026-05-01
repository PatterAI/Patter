import { describe, it, expect } from 'vitest';
import type { CallControl } from '../src/metrics';
import type { ToolDefinition } from '../src/types';

describe('CallControl interface', () => {
  it('can be implemented with required fields', () => {
    const ctrl: CallControl = {
      callId: 'CA123',
      caller: '+1111',
      callee: '+2222',
      transfer: async (number: string) => {
        expect(number).toBe('+3333');
      },
      hangup: async () => {},
    };

    expect(ctrl.callId).toBe('CA123');
    expect(ctrl.caller).toBe('+1111');
    expect(ctrl.callee).toBe('+2222');
  });

  it('transfer and hangup are callable', async () => {
    let transferred = false;
    let hungUp = false;

    const ctrl: CallControl = {
      callId: 'CA456',
      caller: '+1111',
      callee: '+2222',
      transfer: async (number: string) => {
        transferred = true;
      },
      hangup: async () => {
        hungUp = true;
      },
    };

    await ctrl.transfer('+5555');
    expect(transferred).toBe(true);

    await ctrl.hangup();
    expect(hungUp).toBe(true);
  });
});

describe('ToolDefinition with local handler', () => {
  it('supports handler function alongside webhookUrl', () => {
    const tool: ToolDefinition = {
      name: 'lookup_order',
      description: 'Look up an order by ID',
      parameters: {
        type: 'object',
        properties: { order_id: { type: 'string' } },
        required: ['order_id'],
      },
      webhookUrl: 'https://example.com/api/orders',
      handler: async (args: Record<string, unknown>) => {
        return `Order ${args.order_id} found`;
      },
    };

    expect(tool.handler).toBeDefined();
    expect(tool.webhookUrl).toBe('https://example.com/api/orders');
  });

  it('handler is optional', () => {
    const tool: ToolDefinition = {
      name: 'search',
      description: 'Search',
      parameters: {},
      webhookUrl: 'https://example.com/search',
    };

    expect(tool.handler).toBeUndefined();
  });
});
