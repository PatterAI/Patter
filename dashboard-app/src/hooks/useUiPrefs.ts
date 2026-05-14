// Local-only UI preferences: PII reveal toggle + dark-mode toggle.
//
// Both prefs persist in localStorage so the operator's last choice
// survives a page reload. ``revealed`` defaults to ``false`` (PII hidden)
// so a freshly-opened dashboard is screen-share safe by default. ``dark``
// defaults to ``false`` (light theme) to match the brand's cream palette.

import { useCallback, useEffect, useState } from 'react';

const REVEAL_KEY = 'patter.dashboard.reveal';
const THEME_KEY = 'patter.dashboard.theme'; // 'light' | 'dark'

function readBool(key: string, fallback: boolean): boolean {
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === '1' || raw === 'true') return true;
    if (raw === '0' || raw === 'false') return false;
    return fallback;
  } catch {
    return fallback;
  }
}

function readTheme(): 'light' | 'dark' {
  try {
    const raw = window.localStorage.getItem(THEME_KEY);
    if (raw === 'dark') return 'dark';
    if (raw === 'light') return 'light';
  } catch {
    // ignore
  }
  return 'light';
}

export interface UiPrefs {
  /** When true, render full PII (phone numbers, etc.) — eye-OPEN state. */
  readonly revealed: boolean;
  /** When true, force the ``body.dark`` theme. */
  readonly dark: boolean;
  readonly toggleRevealed: () => void;
  readonly toggleDark: () => void;
}

export function useUiPrefs(): UiPrefs {
  const [revealed, setRevealed] = useState<boolean>(() =>
    readBool(REVEAL_KEY, false),
  );
  const [theme, setTheme] = useState<'light' | 'dark'>(() => readTheme());

  // Persist + apply theme to document.body so the existing
  // ``body.dark`` CSS overrides flip in lockstep with the toggle.
  useEffect(() => {
    try {
      window.localStorage.setItem(REVEAL_KEY, revealed ? '1' : '0');
    } catch {
      // ignore quota / privacy-mode errors — the toggle still works for
      // the current session, it just doesn't persist.
    }
  }, [revealed]);

  useEffect(() => {
    try {
      window.localStorage.setItem(THEME_KEY, theme);
    } catch {
      // ignore
    }
    const cls = document.body.classList;
    if (theme === 'dark') cls.add('dark');
    else cls.remove('dark');
  }, [theme]);

  const toggleRevealed = useCallback(() => {
    setRevealed((v) => !v);
  }, []);
  const toggleDark = useCallback(() => {
    setTheme((t) => (t === 'dark' ? 'light' : 'dark'));
  }, []);

  return {
    revealed,
    dark: theme === 'dark',
    toggleRevealed,
    toggleDark,
  };
}
