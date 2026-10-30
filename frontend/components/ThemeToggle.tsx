"use client";

import { useEffect, useState } from "react";

export function ThemeToggle() {
  const [dark, setDark] = useState<boolean | null>(null);

  useEffect(() => {
    setDark(document.documentElement.classList.contains("dark"));
  }, []);

  function toggle() {
    const next = !dark;
    setDark(next);
    document.documentElement.classList.toggle("dark", next);
    try {
      localStorage.setItem("theme", next ? "dark" : "light");
    } catch {
      /* storage unavailable: theme still applies for this page view */
    }
  }

  return (
    <button
      type="button"
      onClick={toggle}
      className="rounded-md border border-line px-2.5 py-1 text-sm text-muted hover:text-fg"
      aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
    >
      {dark === null ? "Theme" : dark ? "Light" : "Dark"}
    </button>
  );
}
