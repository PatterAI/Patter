/**
 * Interactive terminal test mode for voice agents.
 *
 * Simulates a phone call without telephony, STT, or TTS - pure text
 * input/output in the terminal. Useful for rapid agent development.
 */

import { createInterface } from 'readline';
import type { AgentOptions, PipelineMessageHandler } from './types';
import type { CallControl } from './metrics';
import { LLMLoop } from './llm-loop';
import { getLogger } from './logger';

export class TestSession {
  async run(opts: {
    agent: AgentOptions;
    openaiKey?: string;
    onMessage?: PipelineMessageHandler;
    onCallStart?: (data: Record<string, unknown>) => Promise<void>;
    onCallEnd?: (data: Record<string, unknown>) => Promise<void>;
  }): Promise<void> {
    const { agent, openaiKey, onMessage, onCallStart, onCallEnd } = opts;

    const callId = `test_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
    const caller = '+15550000001';
    const callee = '+15550000002';
    const conversationHistory: Array<{ role: string; text: string; timestamp: number }> = [];

    const log = getLogger();
    log.info('');
    log.info('='.repeat(60));
    log.info('  PATTER TEST MODE');
    log.info('='.repeat(60));
    log.info(`  Agent: ${agent.model || 'default'} / ${agent.voice || 'default'}`);
    log.info(`  Provider: ${agent.provider || 'openai_realtime'}`);
    log.info(`  Call ID: ${callId}`);
    log.info(`  Caller: ${caller}  ->  Callee: ${callee}`);
    log.info('-'.repeat(60));
    log.info('  Commands: /quit  /transfer <number>  /hangup  /history');
    log.info('='.repeat(60));
    log.info('');

    // Fire onCallStart
    if (onCallStart) {
      await onCallStart({
        call_id: callId,
        caller,
        callee,
        direction: 'test',
      });
    }

    // Play first message
    if (agent.firstMessage) {
      log.info(`  Agent: ${agent.firstMessage}`);
      log.info('');
      conversationHistory.push({
        role: 'assistant',
        text: agent.firstMessage,
        timestamp: Date.now(),
      });
    }

    // Set up LLM loop if no onMessage and openaiKey is available
    let llmLoop: LLMLoop | null = null;
    if (!onMessage && openaiKey) {
      let llmModel = agent.model || 'gpt-4o-mini';
      if (llmModel.includes('realtime')) llmModel = 'gpt-4o-mini';

      let resolvedPrompt = agent.systemPrompt;
      if (agent.variables) {
        for (const [k, v] of Object.entries(agent.variables)) {
          resolvedPrompt = resolvedPrompt.replaceAll(`{${k}}`, v);
        }
      }

      llmLoop = new LLMLoop(
        openaiKey,
        llmModel,
        resolvedPrompt,
        agent.tools as import('./types').ToolDefinition[] | undefined,
      );
    }

    // Call state
    let ended = false;
    // CallControl for future use (e.g., passing to onMessage)
    const _callControl: CallControl = {
      callId,
      caller,
      callee,
      transfer: async (number: string) => {
        ended = true;
        log.info(`  [Transfer -> ${number}]`);
      },
      hangup: async () => {
        ended = true;
        log.info('  [Call ended by agent]');
      },
    };
    void _callControl;

    // REPL loop
    const rl = createInterface({
      input: process.stdin,
      output: process.stdout,
    });

    const askQuestion = (prompt: string): Promise<string> =>
      new Promise((resolve) => rl.question(prompt, resolve));

    try {
      while (!ended) {
        let userInput: string;
        try {
          userInput = await askQuestion('  You: ');
        } catch {
          log.info('\n  [Session ended]');
          break;
        }

        userInput = userInput.trim();
        if (!userInput) continue;

        // Handle commands
        if (userInput === '/quit') {
          log.info('  [Session ended]');
          break;
        } else if (userInput === '/hangup') {
          log.info('  [You hung up]');
          break;
        } else if (userInput.startsWith('/transfer ')) {
          const number = userInput.slice(10).trim();
          log.info(`  [Transfer -> ${number}]`);
          break;
        } else if (userInput === '/history') {
          for (const entry of conversationHistory) {
            const role = entry.role.charAt(0).toUpperCase() + entry.role.slice(1);
            log.info(`    ${role}: ${entry.text}`);
          }
          continue;
        }

        conversationHistory.push({
          role: 'user',
          text: userInput,
          timestamp: Date.now(),
        });

        // Get response
        if (onMessage) {
          try {
            const responseText = await onMessage({
              text: userInput,
              call_id: callId,
              caller,
              history: [...conversationHistory],
            });
            if (responseText) {
              log.info(`  Agent: ${responseText}`);
              conversationHistory.push({
                role: 'assistant',
                text: responseText,
                timestamp: Date.now(),
              });
              log.info('');
            }
          } catch (e) {
            log.error(`  [Error: ${String(e)}]`);
          }
        } else if (llmLoop) {
          const callCtx = { call_id: callId, caller, callee };
          const parts: string[] = [];
          process.stdout.write('  Agent: ');
          for await (const token of llmLoop.run(userInput, conversationHistory, callCtx)) {
            parts.push(token);
            process.stdout.write(token);
          }
          log.info('');
          const responseText = parts.join('');
          if (responseText) {
            conversationHistory.push({
              role: 'assistant',
              text: responseText,
              timestamp: Date.now(),
            });
          }
          log.info('');
        } else {
          log.info('  [No onMessage handler or LLM loop configured]');
        }

        if (ended) break;
      }
    } finally {
      rl.close();
    }

    // Fire onCallEnd
    if (onCallEnd) {
      await onCallEnd({
        call_id: callId,
        caller,
        callee,
        direction: 'test',
        transcript: conversationHistory,
      });
    }
  }
}
