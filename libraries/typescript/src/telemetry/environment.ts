/**
 * Coarse, anonymous deploy-shape detection for telemetry.
 *
 * Every probe is presence-only: it reads whether a marker env var or file exists
 * and returns a closed enum / boolean — it NEVER returns the value of an env var
 * (which could carry an account id, region, or hostname). Mirrors the emitted
 * enums of `getpatter/telemetry/environment.py` (the detection mechanism may
 * differ per language).
 */

import * as fs from 'node:fs';

const env = process.env;

/** Which AI coding agent (if any) drove this run — presence-only env markers. */
export function invokedByAgent(): string {
  if ('CLAUDECODE' in env || 'CLAUDE_CODE' in env || 'CLAUDE_CODE_ENTRYPOINT' in env)
    return 'claude';
  if ('CURSOR_TRACE_ID' in env || 'CURSOR_AGENT' in env) return 'cursor';
  if ('GITHUB_COPILOT_AGENT' in env || 'COPILOT_AGENT_ID' in env) return 'copilot';
  if ('GEMINI_CLI' in env || 'GEMINI_AGENT' in env) return 'gemini';
  if ('WINDSURF' in env || 'WINDSURF_AGENT' in env) return 'windsurf';
  if ('AIDER' in env || 'OPENAI_AGENT' in env) return 'other';
  return 'none';
}

export function inContainer(): boolean {
  try {
    if (fs.existsSync('/.dockerenv')) return true;
  } catch {
    /* ignore */
  }
  if (env.KUBERNETES_SERVICE_HOST) return true;
  try {
    const blob = fs.readFileSync('/proc/1/cgroup', 'utf8');
    return blob.includes('docker') || blob.includes('containerd') || blob.includes('kubepods');
  } catch {
    return false;
  }
}

export function serverless(): string {
  if (env.AWS_LAMBDA_FUNCTION_NAME) return 'lambda';
  if (env.K_SERVICE) return 'cloud_run';
  if (env.VERCEL) return 'vercel';
  if (env.AZURE_FUNCTIONS_ENVIRONMENT || env.FUNCTIONS_WORKER_RUNTIME) return 'azure_functions';
  return 'none';
}

export function cloud(): string {
  if (env.AWS_REGION || env.AWS_EXECUTION_ENV || env.AWS_LAMBDA_FUNCTION_NAME) return 'aws';
  if (env.K_SERVICE || env.GOOGLE_CLOUD_PROJECT || env.GCP_PROJECT) return 'gcp';
  if (env.WEBSITE_INSTANCE_ID || env.AZURE_FUNCTIONS_ENVIRONMENT) return 'azure';
  if (env.FLY_APP_NAME) return 'fly';
  return 'none';
}

/** Package manager from `npm_config_user_agent` (set when run via a pm script). */
export function packageManager(): string {
  const ua = env.npm_config_user_agent ?? '';
  if (ua.startsWith('pnpm')) return 'pnpm';
  if (ua.startsWith('yarn')) return 'yarn';
  if (ua.startsWith('bun')) return 'bun';
  if (ua.startsWith('npm')) return 'npm';
  return 'none';
}
